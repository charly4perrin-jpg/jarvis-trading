"""Contrôle du sur-apprentissage : un bon résultat est-il réel ou dû au hasard ?

Deux méthodes publiées (Bailey et López de Prado), réécrites ici à partir des formules :

1. Sharpe dégonflé (Deflated Sharpe Ratio, 2014). Quand on essaie N configurations, la
   meilleure a un Sharpe élevé même si aucune n'a d'avantage réel. Le Sharpe dégonflé
   est la probabilité que le vrai Sharpe soit positif, une fois retiré :
   - le Sharpe maximal attendu par pur hasard parmi N essais ;
   - l'effet d'une distribution des rendements asymétrique ou à queues épaisses ;
   - la courte durée de l'historique.
   Au-dessus de 0,95 : l'avantage résiste au nombre d'essais.

2. Probabilité de sur-apprentissage (PBO, validation croisée combinatoire, 2017). On coupe
   l'historique en S blocs. Pour chaque façon de prendre la moitié des blocs comme
   « apprentissage », on choisit la meilleure configuration sur cette moitié, puis on
   regarde son rang sur l'autre moitié, jamais vue. PBO = part des cas où la configuration
   choisie finit dans la moitié basse. Sous 0,5 : le choix apporte quelque chose ;
   proche de 0,5 ou plus : le classement ne se maintient pas, c'est du hasard.
"""
import math
from itertools import combinations
from statistics import NormalDist, mean

EULER = 0.5772156649015329
N01 = NormalDist()


def returns_of(curve: list[float]) -> list[float]:
    return [b / a - 1 for a, b in zip(curve, curve[1:]) if a > 0]


def sharpe_per_period(returns: list[float]) -> float | None:
    if len(returns) < 3:
        return None
    m = mean(returns)
    sd = math.sqrt(sum((r - m) ** 2 for r in returns) / (len(returns) - 1))
    return m / sd if sd > 0 else None


def moments(returns: list[float]) -> tuple[float, float]:
    """Asymétrie et aplatissement (non centré : 3 pour une loi normale)."""
    n = len(returns)
    m = mean(returns)
    s2 = sum((r - m) ** 2 for r in returns) / n
    if s2 == 0:
        return 0.0, 3.0
    s = math.sqrt(s2)
    return (sum((r - m) ** 3 for r in returns) / n / s ** 3,
            sum((r - m) ** 4 for r in returns) / n / s2 ** 2)


def expected_max_sharpe(trial_sharpes: list[float], n_trials: int | None = None) -> float:
    """Sharpe maximal attendu par hasard parmi N essais (formule de Bailey et López de Prado)."""
    n = n_trials or len(trial_sharpes)
    if n < 2 or len(trial_sharpes) < 2:
        return 0.0
    m = mean(trial_sharpes)
    var = sum((s - m) ** 2 for s in trial_sharpes) / (len(trial_sharpes) - 1)
    return math.sqrt(var) * ((1 - EULER) * N01.inv_cdf(1 - 1 / n) + EULER * N01.inv_cdf(1 - 1 / (n * math.e)))


def deflated_sharpe(returns: list[float], trial_sharpes: list[float], n_trials: int | None = None) -> dict:
    """Probabilité que le vrai Sharpe de `returns` dépasse le meilleur Sharpe attendu par
    hasard. `trial_sharpes` : Sharpe (par période) de toutes les configurations essayées."""
    sr = sharpe_per_period(returns)
    if sr is None:
        return {"dsr": None, "sharpe": None, "sharpe0": None, "n_trials": n_trials or len(trial_sharpes)}
    sr0 = expected_max_sharpe(trial_sharpes, n_trials)
    skew, kurt = moments(returns)
    t = len(returns)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    z = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom) if denom > 0 else float("-inf")
    return {"dsr": N01.cdf(z), "sharpe": sr, "sharpe0": sr0, "n_trials": n_trials or len(trial_sharpes),
            "observations": t, "skew": skew, "kurtosis": kurt}


def pbo(returns_matrix: dict[str, list[float]], blocks: int = 10) -> dict:
    """Probabilité de sur-apprentissage par validation croisée combinatoire.
    `returns_matrix` : {configuration: rendements par période}, toutes de même longueur."""
    names = list(returns_matrix)
    t = min(len(v) for v in returns_matrix.values())
    if len(names) < 2 or t < blocks * 3:
        return {"pbo": None, "combinations": 0, "configurations": len(names)}
    edges = [round(i * t / blocks) for i in range(blocks + 1)]
    chunks = list(zip(edges, edges[1:]))

    def sr(name, idx):
        series = [r for k in idx for r in returns_matrix[name][chunks[k][0]:chunks[k][1]]]
        value = sharpe_per_period(series)
        return value if value is not None else -1e9

    below, logits, picks = 0, [], {}
    for train in combinations(range(blocks), blocks // 2):
        test = [k for k in range(blocks) if k not in train]
        best = max(names, key=lambda n: sr(n, train))
        picks[best] = picks.get(best, 0) + 1
        oos = sorted(names, key=lambda n: sr(n, test))
        rank = oos.index(best) + 1                      # 1 = pire, N = meilleur
        omega = rank / (len(names) + 1)
        logit = math.log(omega / (1 - omega))
        logits.append(logit)
        below += logit <= 0
    return {"pbo": below / len(logits), "combinations": len(logits), "configurations": len(names),
            "blocks": blocks, "median_logit": sorted(logits)[len(logits) // 2],
            "most_chosen": max(picks, key=picks.get), "chosen_share": max(picks.values()) / len(logits)}


async def main(args) -> None:
    """Commande `overfitting` : rejoue chaque stratégie en portefeuille partagé, puis mesure
    le Sharpe dégonflé de la stratégie choisie et la probabilité de sur-apprentissage."""
    import asyncio
    from trading_agent.config import Settings
    from trading_agent.evaluate_cli import load_histories
    from trading_agent.exchanges import PublicExchange
    from trading_agent.portfolio_backtest import run_portfolio
    from trading_agent.strategies import STRATEGIES
    settings = Settings()
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    async with PublicExchange("bitvavo") as ex:
        try:
            histories = await load_histories(ex, symbols, args.timeframe, args.days)
        except ValueError as exc:
            raise SystemExit(f"{exc} Allongez la période (--days).")
        tf = ex.timeframe_seconds(args.timeframe)
    data = {k: v["candles"] for k, v in histories.items()}
    rets = {}
    for name in STRATEGIES:
        r = await asyncio.to_thread(run_portfolio, data, settings, tf, [name])
        rets[name] = returns_of(r["equity_curve"])
    sharpes = {n: sharpe_per_period(x) for n, x in rets.items()}
    per_year = math.sqrt(365 * 86_400 / tf)
    print(f"SUR-APPRENTISSAGE, {len(symbols)} marchés, bougies {args.timeframe}, {args.days} jours")
    for n, v in sorted(sharpes.items(), key=lambda kv: -(kv[1] if kv[1] is not None else -9)):
        print(f"  {STRATEGIES[n].label:<24} Sharpe annualisé {'n/a' if v is None else f'{v * per_year:+.2f}'}")
    d = deflated_sharpe(rets[args.strategy], [v for v in sharpes.values() if v is not None], args.trials)
    verdict = "avantage qui résiste au nombre d'essais" if (d["dsr"] or 0) >= 0.95 else "avantage NON prouvé"
    print(f"\nSharpe dégonflé de {args.strategy} ({args.trials} essais comptés) : {d['dsr']:.2f} -> {verdict} (seuil 0,95)")
    p = pbo(rets, blocks=10)
    print(f"Probabilité de sur-apprentissage : {p['pbo']:.2f} (sous 0,5 : le choix tient hors échantillon), "
          f"stratégie la plus souvent choisie : {p['most_chosen']} ({p['chosen_share']:.0%})")

