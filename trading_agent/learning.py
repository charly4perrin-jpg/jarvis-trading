"""Learning Manager : amélioration contrôlée des stratégies.

Ce qu'il peut faire :
- proposer de nouveaux réglages pour UNE stratégie (seulement ses propres paramètres
  numériques, et au plus de 50 % à 200 % de la valeur d'origine) ;
- les tester dans un environnement isolé : backtests sur l'historique, jamais sur le
  portefeuille ni sur l'essai en cours ;
- conserver chaque version, pour pouvoir revenir en arrière.

Ce qu'il ne peut pas faire :
- toucher aux règles de risque, aux limites ou à la configuration ;
- appliquer lui-même un réglage : l'application et le retour en arrière sont des
  décisions humaines, inscrites au journal.

Contre le sur-apprentissage : l'historique est coupé en deux. Les réglages sont
choisis sur les deux premiers tiers ; seul le dernier tiers, jamais vu pendant le
choix, décide. Un réglage n'est retenu que s'il gagne sur ce dernier tiers ET y fait
mieux que le réglage actuel. Le nombre de réglages essayés est noté : plus on en
essaie, plus un bon résultat peut être dû au hasard.
"""
import inspect
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from trading_agent.config import Settings
from trading_agent.strategies import STRATEGIES, Strategy, get_strategy

MIN_RATIO, MAX_RATIO = 0.5, 2.0
STEPS = (0.75, 1.25)
TRAIN_SHARE = 2 / 3
MIN_TRAIN_TRADES, MIN_TEST_TRADES = 20, 10
# Budget d'essais par stratégie, toutes propositions confondues : au-delà, un bon
# résultat a trop de chances d'être dû au hasard (idée reprise de PumpGrok).
MAX_TRIALS = 20


def tunable(strategy: Strategy) -> dict:
    """Paramètres numériques réglables : arguments par défaut des fonctions d'entrée et
    de sortie, préfixés par « entry. » ou « exit. »."""
    out = {}
    for prefix, fn in (("entry", strategy.entry), ("exit", strategy.exit)):
        fn = fn.func if isinstance(fn, partial) else fn
        for name, p in inspect.signature(fn).parameters.items():
            if isinstance(p.default, (int, float)) and not isinstance(p.default, bool):
                out[f"{prefix}.{name}"] = p.default
    return out


def validate(strategy: Strategy, params: dict) -> None:
    base = tunable(get_strategy(strategy.name))
    forbidden = set(Settings.model_fields)
    for key, value in params.items():
        if key not in base or key.split(".", 1)[-1] in forbidden:
            raise PermissionError(f"Paramètre non réglable : {key}.")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise PermissionError(f"Valeur invalide pour {key} : {value}.")
        if not MIN_RATIO <= value / base[key] <= MAX_RATIO:
            raise PermissionError(f"{key} = {value} sort de la plage permise "
                                  f"({base[key] * MIN_RATIO:g} à {base[key] * MAX_RATIO:g}).")


def with_params(strategy: Strategy, params: dict) -> Strategy:
    if not params:
        return strategy
    validate(strategy, params)
    entry = {k.split(".", 1)[1]: v for k, v in params.items() if k.startswith("entry.")}
    exit_ = {k.split(".", 1)[1]: v for k, v in params.items() if k.startswith("exit.")}
    return replace(strategy, entry=partial(strategy.entry, **entry) if entry else strategy.entry,
                   exit=partial(strategy.exit, **exit_) if exit_ else strategy.exit)


def candidates(strategy: Strategy) -> list[dict]:
    """Un paramètre à la fois, à 75 % et 125 % de sa valeur."""
    out = []
    for key, value in tunable(strategy).items():
        for step in STEPS:
            new = value * step
            new = max(1, round(new)) if isinstance(value, int) else round(new, 4)
            if new != value:
                out.append({key: new})
    return out


def split(histories: dict) -> tuple[dict, dict]:
    train, test = {}, {}
    for symbol, h in histories.items():
        cut = int(len(h["candles"]) * TRAIN_SHARE)
        train[symbol] = {**h, "candles": h["candles"][:cut]}
        test[symbol] = {**h, "candles": h["candles"][cut:]}
    return train, test


def propose(name: str, histories: dict, settings: Settings, tf_seconds: int,
            current: dict | None = None, previous_trials: int = 0) -> dict:
    """Cherche un meilleur réglage pour `name`, en environnement isolé."""
    from trading_agent.evaluate import evaluate_strategy
    base = get_strategy(name)
    current = current or {}
    train, test = split(histories)

    def score(params, data):
        r = evaluate_strategy(data, settings, tf_seconds, with_params(base, params), windows=3)
        return {"trades": r["trades"], "expectancy": r["expectancy"], "profit_factor": r["profit_factor"],
                "max_drawdown": r["max_drawdown"]}
    baseline = {"params": current, "train": score(current, train), "test": score(current, test)}
    tried = []
    for change in candidates(with_params(base, current)):
        params = {**current, **change}
        try:
            validate(base, params)
        except PermissionError:
            continue
        tried.append({"params": params, "train": score(params, train)})
    eligible = [t for t in tried if t["train"]["trades"] >= MIN_TRAIN_TRADES and t["train"]["expectancy"] is not None]
    best = max(eligible, key=lambda t: t["train"]["expectancy"], default=None)
    reasons = []
    cumulative = previous_trials + len(tried)
    if cumulative > MAX_TRIALS:
        reasons.append(f"budget d'essais dépassé : {cumulative} réglages essayés au total sur cette "
                       f"stratégie, pour {MAX_TRIALS} permis ; un bon résultat pourrait être dû au hasard")
    if best is None:
        reasons.append("aucun réglage n'a assez de trades sur la période d'apprentissage")
    else:
        best["test"] = score(best["params"], test)
        bt, ct = best["test"], baseline["test"]
        if bt["trades"] < MIN_TEST_TRADES:
            reasons.append(f"seulement {bt['trades']} trades sur la période de contrôle")
        if bt["expectancy"] is None or bt["expectancy"] <= 0:
            reasons.append("perd de l'argent sur la période de contrôle")
        if ct["expectancy"] is not None and (bt["expectancy"] or -1e9) <= ct["expectancy"]:
            reasons.append("ne fait pas mieux que le réglage actuel sur la période de contrôle")
        if (bt["profit_factor"] or 0) < 1:
            reasons.append("ratio gains/pertes sous 1 sur la période de contrôle")
    return {"id": uuid.uuid4().hex[:8], "strategy": name, "created_at": datetime.now(timezone.utc).isoformat(),
            "symbols": list(histories), "tf_seconds": tf_seconds, "tried": len(tried),
            "cumulative_trials": previous_trials + len(tried),
            "baseline": baseline, "best": best, "accepted": best is not None and not reasons,
            "reasons": reasons, "status": "proposée" if best is not None and not reasons else "rejetée"}


class ParamStore:
    """Versions des réglages : propositions, réglage actif, historique."""

    def __init__(self, directory: str):
        self.dir = Path(directory)
        (self.dir / "versions").mkdir(parents=True, exist_ok=True)
        self.active_path = self.dir / "active.json"

    def _read(self, path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write(self, path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def save(self, proposal: dict) -> None:
        self._write(self.dir / "versions" / f"{proposal['id']}.json", proposal)

    def trials(self, name: str) -> int:
        """Nombre total de réglages déjà essayés sur cette stratégie."""
        return sum(v.get("tried", 0) for v in self.versions() if v.get("strategy") == name)

    def versions(self) -> list[dict]:
        items = [self._read(p, None) for p in (self.dir / "versions").glob("*.json")]
        return sorted([i for i in items if i], key=lambda v: v["created_at"], reverse=True)

    def active(self) -> dict:
        return self._read(self.active_path, {})

    def params(self, name: str) -> dict:
        return (self.active().get(name) or {}).get("params", {})

    def apply(self, version_id: str, by: str) -> dict:
        if not by:
            raise PermissionError("Seule une personne peut appliquer un réglage.")
        proposal = self._read(self.dir / "versions" / f"{version_id}.json", None)
        if not proposal:
            raise ValueError(f"Version inconnue : {version_id}.")
        if not proposal["accepted"]:
            raise PermissionError("Ce réglage n'a pas passé le contrôle : " + " ; ".join(proposal["reasons"]) + ".")
        params = proposal["best"]["params"]
        validate(get_strategy(proposal["strategy"]), params)
        data = self.active()
        entry = data.get(proposal["strategy"]) or {"history": []}
        entry["history"].append({"version": entry.get("version"), "params": entry.get("params", {}),
                                 "replaced_at": datetime.now(timezone.utc).isoformat()})
        entry.update({"version": version_id, "params": params, "by": by,
                      "applied_at": datetime.now(timezone.utc).isoformat()})
        data[proposal["strategy"]] = entry
        self._write(self.active_path, data)
        return entry

    def rollback(self, name: str, by: str) -> dict:
        """Revient au réglage précédent (ou d'origine)."""
        if not by:
            raise PermissionError("Seule une personne peut revenir en arrière.")
        get_strategy(name)
        data = self.active()
        entry = data.get(name)
        if not entry or not entry.get("history"):
            raise ValueError(f"{name} utilise déjà son réglage d'origine.")
        previous = entry["history"].pop()
        entry.update({"version": previous["version"], "params": previous["params"], "by": by,
                      "applied_at": datetime.now(timezone.utc).isoformat()})
        data[name] = entry
        self._write(self.active_path, data)
        return entry


def effective(name: str, store: ParamStore | None) -> Strategy:
    """La stratégie avec son réglage actif (celui d'origine par défaut)."""
    base = get_strategy(name)
    try:
        return with_params(base, store.params(name)) if store else base
    except PermissionError:
        return base   # réglage devenu invalide : on revient à l'origine, par prudence
