"""Démo rejouable de Jarvis : hors ligne, argent fictif, données préparées.

Les DONNÉES sont préparées (horloge, bougies et carnets fixés, repris des scénarios
extrêmes) ; les DÉCISIONS ne le sont pas : chaque étape passe par le vrai Coordinator,
le vrai moteur de risque et le vrai courtier papier. Aucun réseau, aucun .env, aucun
appel IA, aucune notification. Tout est écrit dans un dossier dédié (data/demo_run/).
"""
import asyncio
import shutil
from pathlib import Path

from trading_agent.agents import Coordinator
from trading_agent.config import Settings
from trading_agent.events import EventBus
from trading_agent.paper import PaperBroker
from trading_agent.scenarios import BUY, MIN, SimExchange

BANNER = "Démo : données préparées, argent fictif"
DEFAULT_DIR = "data/demo_run"
MARKER = ".jarvis_demo"          # prouve que le dossier a été créé par la démo
EXCHANGE, SYMBOL, TF = "demo", "BTC/EUR", "1m"
REPO = Path(__file__).resolve().parents[1]

# Étape 1 : signal présent, bougies fraîches, mais le carnet fourni par la « plateforme »
# date d'une heure : le moteur de risque refuse l'achat (carnet périmé).
STALE = dict(closes=BUY, book_ts_ms=len(BUY) * MIN - 3_600_000)
# Étape 2 : même croisement haussier, sur une bougie plus récente, données fraîches.
FRESH = [100.0] * 31 + [130.0]
# Étape 3 : la bougie suivante ouvre sous le stop (scénario « Krach éclair »).
CRASH = [[i * MIN, c, c + 1, c - 1, c, 1.0] for i, c in enumerate(FRESH)] + [
    [len(FRESH) * MIN, 92.0, 93.0, 88.0, 90.0, 50.0]]
# Étape 4 : après le krach, le marché se stabilise à 90 €, puis un nouveau croisement
# haussier à 100 € ; le prix monte jusqu'à 121 € puis redescend lentement. Jarvis
# vérifie chaque nouvelle bougie ; sa règle de sortie décide seule du moment de vendre.
WIN_BUY = [c[4] for c in CRASH] + [90.0] * 25 + [100.0]
WIN_PATH = [103.0, 106.0, 109.0, 112.0, 115.0, 118.0, 120.0, 121.0, 121.0, 120.0] + [
    120.0 - 1.5 * i for i in range(1, 30)]


def _path_fields() -> list[str]:
    return [n for n in Settings.model_fields if n.endswith("_path") or n.endswith("_dir")]


def main_trial_paths() -> set[Path]:
    """Fichiers et dossiers de l'essai principal (valeurs par défaut, lues sans .env)."""
    out = set()
    for root in {Path.cwd(), REPO}:
        for name in _path_fields():
            out.add((root / Settings.model_fields[name].default).resolve())
    return out


def check_isolation(demo_dir: Path, paths: list[str]) -> None:
    """Refuse de démarrer si un chemin de la démo touche l'essai principal."""
    demo_dir = demo_dir.resolve()
    clashes = []
    for main in main_trial_paths():
        if demo_dir == main or demo_dir in main.parents:
            clashes.append(str(main))
        for p in paths:
            p = Path(p).resolve()
            if p == main or main in p.parents:
                clashes.append(f"{p} → {main}")
    if clashes:
        raise ValueError("Démo refusée : chemin partagé avec l'essai principal : "
                         + " ; ".join(sorted(set(clashes))))


def demo_settings(demo_dir: Path, cash: float = 3_000) -> Settings:
    paths = {n: str(demo_dir / Path(Settings.model_fields[n].default).name) for n in _path_fields()}
    settings = Settings(
        _env_file=None, app_env="paper", strategy="momentum_demo", regime_filter=False,
        paper_starting_cash=cash, news_enabled=False, notify_macos=False, notify_email_to=None,
        notify_daily_summary=False, xai_api_key=None, openai_api_key=None, smtp_password=None,
        **paths)
    settings.validate_safety()
    check_isolation(demo_dir, list(paths.values()))
    return settings


def prepare_dir(demo_dir: Path) -> None:
    """Repart d'un dossier vide. N'efface qu'un dossier créé par la démo elle-même."""
    if demo_dir.exists() and any(demo_dir.iterdir()):
        if not (demo_dir / MARKER).exists():
            raise ValueError(f"Démo refusée : {demo_dir} n'est pas un dossier de démo (non vide).")
        shutil.rmtree(demo_dir)
    demo_dir.mkdir(parents=True, exist_ok=True)
    (demo_dir / MARKER).write_text("dossier créé par python -m trading_agent demo\n", encoding="utf-8")


def _first(bus, corr, type_):
    return next((e for e in bus.thread(corr) if e.type == type_), None)


async def run_demo(demo_dir: str | Path = DEFAULT_DIR, cash: float = 3_000) -> dict:
    if not (cash == cash and 100 <= cash <= 1_000_000):
        raise ValueError("Démo refusée : capital fictif entre 100 € et 1 000 000 €.")
    demo_dir = Path(demo_dir)
    check_isolation(demo_dir, [])          # avant d'écrire quoi que ce soit
    prepare_dir(demo_dir)
    s = demo_settings(demo_dir, cash)
    bus = EventBus(s.event_log_path, s.halt_flag_path, s.database_path)
    broker = PaperBroker(s.paper_starting_cash, s.paper_fee_rate, s.paper_slippage_bps,
                         s.paper_ledger_path, s.paper_state_path, s.paper_max_slippage_bps)
    c = Coordinator(bus, s, broker)
    steps = []

    cash0 = broker.portfolio.cash
    corr = await c.run_cycle(SimExchange(**STALE), EXCHANGE, SYMBOL, TF)
    risk, book = _first(bus, corr, "risk.decision"), _first(bus, corr, "market.order_book")
    steps.append({"step": "refus", "thread": corr, "signal": _first(bus, corr, "strategy.signal") is not None,
                  "approved": risk.payload["approved"] if risk else None,
                  "reason": risk.result if risk else None,
                  "data_age_s": _first(bus, corr, "market.snapshot").payload["data_age_s"],
                  "book_age_s": ((book.payload["read_ms"] - book.payload["book_ms"]) / 1000) if book else None,
                  "book_best_ask": book.payload["best_ask"] if book else None,
                  "cash_before": cash0, "cash_after": broker.portfolio.cash,
                  "outcome": _first(bus, corr, "cycle.end").result})

    corr = await c.run_cycle(SimExchange(FRESH), EXCHANGE, SYMBOL, TF)
    fill, book = _first(bus, corr, "execution.fill"), _first(bus, corr, "market.order_book")
    pos = broker.portfolio.positions.get(SYMBOL)
    steps.append({"step": "achat", "thread": corr, "approved": _first(bus, corr, "risk.decision").payload["approved"],
                  "book_asks": book.payload["asks"] if book else None,
                  "fill_price": fill.payload["fill_price"] if fill else None,
                  "quantity": fill.payload["quantity"] if fill else None,
                  "fee": fill.payload["fee"] if fill else None,
                  "stop_price": pos["stop_price"] if pos else None,
                  "cash_after": broker.portfolio.cash, "position_open": pos is not None,
                  "outcome": _first(bus, corr, "cycle.end").result})

    corr = await c.run_cycle(SimExchange([90.0] * len(CRASH), candles=CRASH), EXCHANGE, SYMBOL, TF)
    fill = _first(bus, corr, "execution.fill")
    steps.append({"step": "stop", "thread": corr,
                  "reason": fill.payload["reason"] if fill else None,
                  "fill_price": fill.payload["fill_price"] if fill else None,
                  "quantity": fill.payload["quantity"] if fill else None,
                  "fee": fill.payload["fee"] if fill else None,
                  "realized_pnl": fill.payload["realized_pnl"] if fill else None,
                  "cash_after": broker.portfolio.cash,
                  "position_open": SYMBOL in broker.portfolio.positions,
                  "outcome": _first(bus, corr, "cycle.end").result})

    corr = await c.run_cycle(SimExchange(WIN_BUY), EXCHANGE, SYMBOL, TF)
    buy = _first(bus, corr, "execution.fill")
    win = {"step": "gain", "thread": corr, "approved": _first(bus, corr, "risk.decision").payload["approved"]
           if _first(bus, corr, "risk.decision") else None,
           "buy_price": buy.payload["fill_price"] if buy else None,
           "quantity": buy.payload["quantity"] if buy else None,
           "buy_fee": buy.payload["fee"] if buy else None,
           "stop_price": (broker.portfolio.positions.get(SYMBOL) or {}).get("stop_price"),
           "exit_thread": None, "fill_price": None, "fee": None, "realized_pnl": None, "reason": None,
           "candles_watched": 0, "peak": None}
    if buy:
        for k in range(1, len(WIN_PATH) + 1):
            corr = await c.run_cycle(SimExchange(WIN_BUY + WIN_PATH[:k]), EXCHANGE, SYMBOL, TF)
            fill = _first(bus, corr, "execution.fill")
            win["candles_watched"], win["peak"] = k, max(WIN_PATH[:k])
            if fill or bus.halted:
                break
        if fill:
            win.update(exit_thread=corr, fill_price=fill.payload["fill_price"], fee=fill.payload["fee"],
                       realized_pnl=fill.payload["realized_pnl"], reason=fill.payload["reason"],
                       exit_quantity=fill.payload["quantity"])
    win.update(cash_after=broker.portfolio.cash, position_open=SYMBOL in broker.portfolio.positions)
    steps.append(win)
    return {"dir": str(demo_dir), "events": s.event_log_path, "start_cash": cash0,
            "final_cash": broker.portfolio.cash, "halted": bus.halted, "steps": steps}


def _e(x) -> str:
    return f"{x:,.2f} €".replace(",", " ").replace(".", ",")


def main(demo_dir: str = DEFAULT_DIR, cash: float = 3_000) -> dict:
    print(f"=== {BANNER} ===\n")
    r = asyncio.run(run_demo(demo_dir, cash))
    a, b, d, g = r["steps"]
    log = r["events"]
    print("1) Proposition refusée")
    print(f"   Signal présent : {'oui' if a['signal'] else 'non'} ; bougies fraîches, mais carnet vieux de "
          f"{(a['book_age_s'] or 0) / 60:.0f} min (meilleure vente {_e(a['book_best_ask'] or 0)})")
    print(f"   Décision du risque : {a['reason']}")
    print(f"   Cash : {_e(a['cash_before'])} avant, {_e(a['cash_after'])} après (inchangé)")
    print(f"   Trace : python -m trading_agent trace --log {log} --thread {a['thread']}\n")
    print("2) Proposition acceptée : achat papier")
    print(f"   Carnet utilisé (ventes) : {b['book_asks']}")
    print(f"   Prix simulé {_e(b['fill_price'])}, quantité {b['quantity']:.8f} BTC, "
          f"frais {_e(b['fee'])}, stop {_e(b['stop_price'])}")
    print(f"   Position ouverte : {'oui' if b['position_open'] else 'non'} ; cash {_e(b['cash_after'])}")
    print(f"   Trace : python -m trading_agent trace --log {log} --thread {b['thread']}\n")
    print("3) Fermeture par le stop (la bougie ouvre sous le stop)")
    print(f"   Prix de sortie {_e(d['fill_price'])}, quantité {d['quantity']:.8f} BTC, "
          f"frais {_e(d['fee'])}, résultat {_e(d['realized_pnl'])}")
    print(f"   Position ouverte : {'oui' if d['position_open'] else 'non'} ; cash {_e(d['cash_after'])}")
    print(f"   Trace : python -m trading_agent trace --log {log} --thread {d['thread']}\n")
    print("4) Trade gagnant : nouveau signal, achat, puis sortie sur signal")
    print(f"   Achat {g['quantity']:.8f} BTC à {_e(g['buy_price'])}, frais {_e(g['buy_fee'])}, "
          f"stop {_e(g['stop_price'])}")
    print(f"   Le prix monte jusqu'à {_e(g['peak'])} puis redescend ; Jarvis vérifie "
          f"{g['candles_watched']} bougies et vend quand sa règle de sortie le dit")
    print(f"   Vente à {_e(g['fill_price'])}, frais {_e(g['fee'])}, résultat {_e(g['realized_pnl'])}")
    print(f"   Traces : achat --thread {g['thread']} ; vente --thread {g['exit_thread']}\n")
    total = r["final_cash"] - r["start_cash"]
    print(f"Bilan : {_e(r['start_cash'])} au départ, {_e(r['final_cash'])} à la fin ({'+' if total >= 0 else ''}"
          f"{_e(total)}) — un trade perdant, un trade gagnant, un refus.")
    print(f"Fichiers de la démo : {r['dir']} (l'essai principal n'est pas touché).")
    return r
