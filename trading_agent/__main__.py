import argparse
import asyncio
from trading_agent.runner import run_compare, run_paper

from trading_agent.strategies import STRATEGIES


def main():
    parser = argparse.ArgumentParser(description="Crypto agent public data + paper trading")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--quote", default="EUR")
    c.add_argument("--min-quote-volume", type=float, default=50_000)
    c.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("paper")
    p.add_argument("--exchange", choices=["bitvavo", "kraken"], required=True)
    p.add_argument("--symbol", default="BTC/EUR")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--cycles", type=int, default=1)
    a = sub.add_parser("agents", help="cycle multi-agents en paper trading")
    a.add_argument("--exchange", choices=["bitvavo", "kraken"], required=True)
    a.add_argument("--symbol", default="BTC/EUR")
    a.add_argument("--timeframe", default="5m")
    a.add_argument("--cycles", type=int, default=1)
    a.add_argument("--strategy", choices=list(STRATEGIES), default=None)
    t = sub.add_parser("trace", help="affiche un fil de décision (le dernier par défaut)")
    t.add_argument("--thread", default=None)
    t.add_argument("--log", default=None, help="journal à lire (ex. data/demo_run/events.jsonl)")
    ho = sub.add_parser("holdings", help="portefeuille réel en lecture seule (aucun compte connecté)")
    ho.add_argument("action", choices=["show", "import", "set"])
    ho.add_argument("csv", nargs="?", help="relevé CSV : plateforme,actif,quantite[,date]")
    ho.add_argument("--exchange", choices=["bitvavo", "kraken"])
    ho.add_argument("--asset", help="ex. BTC")
    ho.add_argument("--quantity", type=float, help="solde détenu (0 = retirer la ligne)")
    ho.add_argument("--date", default=None, help="date du relevé (AAAA-MM-JJ)")
    ho.add_argument("--file", default="data/holdings.json")
    dm = sub.add_parser("demo", help="démo rejouable hors ligne : un refus, un achat papier, un stop")
    dm.add_argument("--dir", default="data/demo_run", help="dossier dédié à la démo")
    dm.add_argument("--cash", type=float, default=3_000, help="capital FICTIF de départ en euros")
    sub.add_parser("verify", help="vérifie que le journal d'événements n'a pas été modifié")
    rs = sub.add_parser("resume-strategy", help="lève la pause d'une stratégie dégradée (décision humaine)")
    rs.add_argument("--strategy", required=True, choices=list(STRATEGIES))
    rs.add_argument("--confirm", required=True, help="retapez le nom de la stratégie pour confirmer")
    pf = sub.add_parser("portfolio", help="rejoue un portefeuille complet (capital partagé entre plusieurs paires)")
    pf.add_argument("--strategies", default="volume_expansion", help="noms séparés par des virgules, par priorité")
    pf.add_argument("--symbols", default="BTC/EUR,ETH/EUR,SOL/EUR,XRP/EUR,ADA/EUR,LINK/EUR,DOGE/EUR,LTC/EUR")
    pf.add_argument("--timeframe", default="1d")
    pf.add_argument("--days", type=int, default=1825)
    ov = sub.add_parser("overfitting", help="Sharpe dégonflé et probabilité de sur-apprentissage")
    ov.add_argument("--strategy", default="volume_expansion")
    ov.add_argument("--trials", type=int, default=30, help="nombre de configurations essayées au total")
    ov.add_argument("--symbols", default="BTC/EUR,ETH/EUR,SOL/EUR,XRP/EUR,ADA/EUR,LINK/EUR,DOGE/EUR,LTC/EUR")
    ov.add_argument("--timeframe", default="1d")
    ov.add_argument("--days", type=int, default=1825)
    sub.add_parser("scenarios", help="simule des scénarios extrêmes (krach, panne, doublons...)")
    lm = sub.add_parser("learning", help="réglages des stratégies : proposer, lister, appliquer, revenir")
    lm.add_argument("action", choices=["propose", "list", "apply", "rollback"])
    lm.add_argument("--strategy", choices=list(STRATEGIES), default=None)
    lm.add_argument("--id", default=None, help="version à appliquer")
    lm.add_argument("--confirm", default=None, help="retapez l'identifiant (apply) ou le nom (rollback)")
    d = sub.add_parser("db", help="état de la base de données et recherche dans l'historique")
    d.add_argument("--sync", action="store_true", help="rattrape la base depuis le journal")
    d.add_argument("--type", default=None, help="préfixe du type d'événement, ex. execution.fill")
    d.add_argument("--symbol", default=None)
    d.add_argument("--limit", type=int, default=20)
    u = sub.add_parser("ui", help="interface Jarvis dans le navigateur")
    u.add_argument("--exchange", choices=["bitvavo", "kraken"], default="bitvavo")
    u.add_argument("--symbol", default="BTC/EUR")
    u.add_argument("--timeframe", default="5m")
    u.add_argument("--interval", type=float, default=None,
                   help="secondes entre deux cycles (par défaut : la durée d'une bougie)")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--also", action="append", choices=["bitvavo", "kraken"], default=[],
                   help="plateforme suivie en plus, avec son propre portefeuille fictif")
    u.add_argument("--strategy", choices=list(STRATEGIES), default=None)
    b = sub.add_parser("backtest", help="rejoue la stratégie sur l'historique")
    b.add_argument("--exchange", choices=["bitvavo", "kraken"], default="bitvavo")
    b.add_argument("--symbol", default="BTC/EUR")
    b.add_argument("--timeframe", default="1h")
    b.add_argument("--days", type=int, default=180)
    b.add_argument("--strategy", choices=list(STRATEGIES) + ["all"], default=None,
                   help="all = comparer toutes les stratégies")
    e = sub.add_parser("evaluate", help="évalue les stratégies sur un panier de marchés")
    e.add_argument("--exchange", choices=["bitvavo", "kraken"], default="bitvavo")
    e.add_argument("--symbols", default="BTC/EUR,ETH/EUR,SOL/EUR")
    e.add_argument("--timeframe", default="4h")
    e.add_argument("--days", type=int, default=730)
    sub.add_parser("status", help="statut de chaque stratégie dans le circuit de validation")
    rc = sub.add_parser("reconcile", help="rejoue les dernières décisions de l'essai et cherche les écarts")
    rc.add_argument("--last", type=int, default=20, help="nombre de cycles à rejouer (0 = tous)")
    rc.add_argument("--all", action="store_true", help="affiche aussi les cycles conformes")
    pr = sub.add_parser("promote", help="fait avancer une stratégie d'un cran (action humaine)")
    pr.add_argument("--strategy", required=True)
    pr.add_argument("--confirm", required=True, help="retapez le nom de la stratégie pour confirmer")
    de = sub.add_parser("demote", help="renvoie une stratégie au statut candidate")
    de.add_argument("--strategy", required=True)
    de.add_argument("--reason", required=True)
    h = sub.add_parser("halt", help="arrêt d'urgence : bloque tout nouvel ordre")
    h.add_argument("--reason", default="")
    sub.add_parser("resume", help="lève l'arrêt d'urgence")
    args = parser.parse_args()
    if args.command == "compare":
        asyncio.run(run_compare(args.quote, args.min_quote_volume, args.limit))
    elif args.command == "agents":
        from trading_agent.agents import run_agents
        asyncio.run(run_agents(args.exchange, args.symbol, args.timeframe, args.cycles, args.strategy))
    elif args.command == "ui":
        from trading_agent.server import serve
        serve(args.exchange, args.symbol, args.timeframe, args.interval, args.port, args.strategy,
              args.also)
    elif args.command == "backtest":
        from trading_agent.backtest import run_backtest_cli
        asyncio.run(run_backtest_cli(args.exchange, args.symbol, args.timeframe, args.days, args.strategy))
    elif args.command in ("evaluate", "status", "promote", "demote"):
        from trading_agent import evaluate_cli
        asyncio.run(evaluate_cli.main(args))
    elif args.command in ("halt", "resume"):
        from trading_agent.agents import set_halt
        set_halt(args.command == "halt", getattr(args, "reason", ""))
    elif args.command == "resume-strategy":
        from trading_agent.agents import resume_strategy
        resume_strategy(args.strategy, args.confirm)
    elif args.command == "portfolio":
        from trading_agent.portfolio_backtest import main as portfolio_main
        asyncio.run(portfolio_main(args))
    elif args.command == "reconcile":
        from trading_agent.reconcile import main as reconcile_main
        asyncio.run(reconcile_main(args))
    elif args.command == "overfitting":
        from trading_agent.overfitting import main as overfitting_main
        asyncio.run(overfitting_main(args))
    elif args.command == "scenarios":
        from trading_agent import scenarios
        from trading_agent.config import Settings
        scenarios.main(Settings().scenarios_path)
    elif args.command == "learning":
        from trading_agent.agents import learning_cli
        asyncio.run(learning_cli(args))
    elif args.command == "db":
        from trading_agent.agents import print_db
        print_db(args)
    elif args.command == "holdings":
        if args.action == "import" and not args.csv:
            parser.error("holdings import : indiquez le fichier CSV")
        if args.action == "set" and (not args.exchange or not args.asset or args.quantity is None):
            parser.error("holdings set : --exchange, --asset et --quantity sont obligatoires")
        from trading_agent.holdings import main as holdings_main
        holdings_main(args)
    elif args.command == "demo":
        from trading_agent.demo_run import main as demo_main
        demo_main(args.dir, args.cash)
    elif args.command in ("trace", "verify"):
        from trading_agent.agents import print_trace, print_verify
        from trading_agent.config import Settings
        path = getattr(args, "log", None) or Settings().event_log_path
        print_trace(path, args.thread) if args.command == "trace" else print_verify(path)
    else:
        asyncio.run(run_paper(args.exchange, args.symbol, args.timeframe, args.cycles))

if __name__ == "__main__":
    main()
