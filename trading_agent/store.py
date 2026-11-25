"""Base de données des événements (SQLite, schéma prêt pour PostgreSQL).

Rôles :
- le journal JSONL chaîné reste la référence infalsifiable : chaque ligne contient
  l'empreinte de la précédente ;
- la base sert à chercher et à compter vite (par fil, type, agent, paire, date)
  sans relire tout le fichier.

Chaque événement est écrit dans les deux. Si la base est indisponible, le journal
continue d'être écrit, la base est rattrapée depuis le journal au prochain
enregistrement réussi, et aucun achat n'est autorisé tant qu'elle est en retard
(fail-closed : on n'agit pas sans pouvoir tout enregistrer).

Passage à PostgreSQL : même table ; `payload` devient JSONB, `seq` devient
BIGSERIAL, `json_extract(payload, '$.x')` devient `payload->>'x'`.
"""
import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    id             TEXT NOT NULL UNIQUE,
    ts             TEXT NOT NULL,
    source         TEXT NOT NULL,
    target         TEXT NOT NULL,
    type           TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    status         TEXT NOT NULL,
    priority       TEXT NOT NULL,
    reply_to       TEXT,
    schema_version INTEGER NOT NULL,
    result         TEXT NOT NULL,
    payload        TEXT NOT NULL,
    duration_ms    REAL,
    symbol         TEXT,
    prev_hash      TEXT NOT NULL,
    hash           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_corr ON events (correlation_id);
CREATE INDEX IF NOT EXISTS events_type_ts ON events (type, ts);
CREATE INDEX IF NOT EXISTS events_source_ts ON events (source, ts);
CREATE INDEX IF NOT EXISTS events_symbol_ts ON events (symbol, ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

COLUMNS = ("id", "ts", "source", "target", "type", "correlation_id", "status", "priority",
           "reply_to", "schema_version", "result", "payload", "duration_ms", "symbol",
           "prev_hash", "hash")


def _row(event) -> tuple:
    data = event.model_dump(mode="json")
    payload = data.get("payload") or {}
    data["payload"] = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    data["symbol"] = payload.get("symbol")
    return tuple(data.get(c) for c in COLUMNS)


class EventStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Une seule connexion partagée entre fils d'exécution : lectures ET écritures
        # passent par ce verrou (sans lui, deux lectures simultanées se mélangent).
        self.lock = threading.RLock()
        self.last_error: str | None = None
        self.behind = False   # vrai si des événements du journal manquent dans la base
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")    # lecture et écriture simultanées
        self.db.executescript(SCHEMA)
        self.db.commit()

    @property
    def ok(self) -> bool:
        return self.last_error is None and not self.behind

    def insert(self, event) -> None:
        """Ajout idempotent : un événement déjà présent n'est pas dupliqué."""
        with self.lock:
            self.db.execute(f"INSERT OR IGNORE INTO events ({', '.join(COLUMNS)}) "
                            f"VALUES ({', '.join('?' * len(COLUMNS))})", _row(event))
            self.db.commit()

    def record(self, event, journal_path: Path | None) -> None:
        """Écrit un événement ; en cas d'échec, note le retard au lieu de planter."""
        try:
            if self.behind and journal_path:
                self.sync_from_journal(journal_path)
            self.insert(event)
            self.last_error = None
        except sqlite3.Error as exc:
            self.last_error, self.behind = str(exc), True

    def sync_from_journal(self, journal_path: Path) -> int:
        """Rattrape la base depuis le journal (ajoute ce qui manque). Retourne le
        nombre d'événements ajoutés."""
        from trading_agent.events import parse_line
        if not Path(journal_path).exists():
            self.behind = False
            return 0
        before = self.count()
        with self.lock, Path(journal_path).open(encoding="utf-8") as f:
            rows = [_row(parse_line(line)) for line in f if line.strip()]
            self.db.executemany(f"INSERT OR IGNORE INTO events ({', '.join(COLUMNS)}) "
                                f"VALUES ({', '.join('?' * len(COLUMNS))})", rows)
            self.db.commit()
        self.behind, self.last_error = False, None
        return self.count() - before

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def search(self, correlation_id: str | None = None, type_prefix: str | None = None,
               source: str | None = None, symbol: str | None = None, status: str | None = None,
               since: str | None = None, until: str | None = None, text: str | None = None,
               limit: int = 200) -> list[dict]:
        """Recherche dans l'historique complet, du plus récent au plus ancien."""
        where, args = [], []
        for column, value in (("correlation_id", correlation_id), ("source", source),
                              ("symbol", symbol), ("status", status)):
            if value:
                where.append(f"{column} = ?")
                args.append(value)
        if type_prefix:
            where.append("type LIKE ?")
            args.append(type_prefix.replace("%", "") + "%")
        if since:
            where.append("ts >= ?")
            args.append(since)
        if until:
            where.append("ts < ?")
            args.append(until)
        if text:
            where.append("result LIKE ?")
            args.append("%" + text.replace("%", "") + "%")
        sql = ("SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY seq DESC LIMIT ?")
        with self.lock:
            rows = self.db.execute(sql, [*args, max(1, min(limit, 5000))]).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def stats(self) -> dict:
        with self.lock:
            return self._stats()

    def _stats(self) -> dict:
        db = self.db
        first_last = db.execute("SELECT MIN(ts), MAX(ts) FROM events").fetchone()
        by_type = db.execute("SELECT type, COUNT(*) n FROM events GROUP BY type ORDER BY n DESC").fetchall()
        threads = db.execute("SELECT COUNT(DISTINCT correlation_id) FROM events").fetchone()[0]
        return {"path": str(self.path), "events": self.count(), "threads": threads,
                "first": first_last[0], "last": first_last[1],
                "by_type": {r["type"]: r["n"] for r in by_type},
                "ok": self.ok, "error": self.last_error}

    def compare_with_journal(self, journal_path: Path) -> dict:
        """La base contient-elle exactement les événements du journal, avec les mêmes empreintes ?"""
        from trading_agent.events import parse_line
        journal = {}
        if Path(journal_path).exists():
            with Path(journal_path).open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        e = parse_line(line)
                        journal[e.id] = e.hash
        with self.lock:
            stored = dict(self.db.execute("SELECT id, hash FROM events").fetchall())
        missing = [i for i in journal if i not in stored]
        extra = [i for i in stored if i not in journal]
        altered = [i for i, h in journal.items() if i in stored and stored[i] != h]
        return {"journal": len(journal), "database": len(stored), "missing": len(missing),
                "extra": len(extra), "altered": len(altered),
                "consistent": not (missing or extra or altered)}

    def close(self) -> None:
        self.db.close()
