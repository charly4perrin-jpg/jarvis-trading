from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "paper"
    quote_currency: str = "EUR"
    min_quote_volume_24h: float = 50_000
    max_spread_bps: float = 40
    paper_starting_cash: float = 3_000
    paper_fee_rate: float = 0.0025
    paper_slippage_bps: float = 10
    paper_max_slippage_bps: float = 50
    risk_per_trade_pct: float = 0.005
    max_daily_loss_pct: float = 0.02
    max_weekly_loss_pct: float = 0.05
    max_total_exposure_pct: float = 0.60
    min_cash_reserve_pct: float = 0.40
    max_open_positions: int = 5
    audit_log_path: str = "./data/audit.jsonl"
    paper_ledger_path: str = "./data/paper_ledger.jsonl"
    paper_state_path: str = "./data/paper_state.json"
    event_log_path: str = "./data/events.jsonl"
    database_path: str = "./data/jarvis.db"
    max_consecutive_losses: int = 3
    # Vitesse de perte : pas de nouvel achat si le capital a baissé de 1,5 % ou plus
    # par rapport à son plus haut des dernières 24 h (fenêtre glissante, pas remise à
    # zéro à minuit). La pause cesse d'elle-même quand le plus haut sort de la fenêtre.
    max_loss_velocity_pct: float = 0.015
    loss_velocity_window_h: float = 24
    strategy: str = "momentum_demo"
    # Ne pas ouvrir de position hors du régime de marché prévu pour la stratégie.
    regime_filter: bool = True
    # Régime de Markov caché (hmm_regime.py) : veto d'entrée seulement quand une phase
    # « agitée » est confirmée avec confiance. Une lecture indéterminée ne bloque rien :
    # le filtre de régime classique reste la barrière principale (et fail-closed).
    hmm_filter: bool = True
    # Le Contradicteur s'applique aussi aux backtests (sauf contrôles du carnet en direct).
    adversarial_in_backtest: bool = True
    # Parcours de décision (phase A).
    reward_risk: float = 2.0              # objectif = 2 fois la distance au stop
    proposal_ttl_s: int = 120             # durée de validité d'une proposition
    adversarial_min_depth_eur: float = 2_000   # profondeur vendeuse minimale à 0,5 %
    watchlist_size: int = 3               # paires suivies sur la plateforme principale
    extra_watchlist_size: int = 3         # paires suivies sur chaque plateforme en plus
    # Phase C : corrélations et plateformes.
    max_correlated_risk_pct: float = 0.01   # risque cumulé max des positions corrélées
    correlation_threshold: float = 0.7      # au-delà, deux positions sont un même risque
    correlation_window: int = 100           # bougies utilisées pour la corrélation
    allowed_quotes: list[str] = ["EUR"]     # USDT/USDC seulement après validation
    extra_exchanges: list[str] = []         # ex. ["kraken"] : plateformes en plus
    extra_exchange_cash: float = 1_000      # capital fictif de chaque plateforme en plus
    # Phase D : performance et pauses.
    pause_path: str = "./data/paused.json"
    # Phase E : qualité d'exécution et arbitrage.
    limit_min_saving_bps: float = 15        # économie minimale pour préférer un ordre limite
    limit_expiry_candles: int = 1           # durée de vie d'un ordre limite
    # Âge maximal du carnet d'ordres au moment où le risque décide d'un ACHAT (secondes).
    # Au-delà, ou si l'âge est inconnu, l'achat est refusé ; les sorties ne sont jamais bloquées.
    max_book_age_s: float = 30
    limit_max_participation: float = 0.10   # part max. du volume échangé sous la limite qu'un ordre peut prendre
    arbitrage_notional_eur: float = 500     # taille de référence pour mesurer un écart
    arbitrage_log_path: str = "./data/arbitrage.jsonl"
    # Phase G.
    shadow_dir: str = "./data/shadow"
    scenarios_path: str = "./data/scenarios.json"
    learning_dir: str = "./data/learning"
    news_enabled: bool = True     # lit les pages d'état officielles des plateformes
    # Grok (API officielle xAI) : avis en langage clair, jamais de décision.
    # La clé se met UNIQUEMENT dans le fichier .env : XAI_API_KEY=...
    xai_api_key: SecretStr | None = None
    xai_model: str = "grok-4.7"
    # ChatGPT (API officielle OpenAI) : clé à mettre dans .env : OPENAI_API_KEY=...
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-6.1-terra"
    ai_provider: str = "auto"     # auto (celui qui a une clé, ChatGPT d'abord), openai ou xai
    ai_max_calls_per_day: int = 40
    ai_budget_path: str = "./data/ai_budget.json"
    ai_cache_path: str = "./data/ai_cache.json"
    performance_dir: str = "./data/performance"
    scan_every_hours: float = 24
    halt_flag_path: str = "./data/HALT"
    lab_dir: str = "./data/lab"
    lab_daily_dir: str = "./data/lab_1d"   # laboratoire en bougies journalières
    breakers_dir: str = "./data/breakers"  # état des coupe-circuits, conservé au redémarrage
    notifications_path: str = "./data/notifications.jsonl"
    registry_path: str = "./data/registry.json"
    evaluations_dir: str = "./data/evaluations"
    # Critères d'acceptation d'une stratégie en backtest.
    accept_min_trades: int = 100
    accept_min_profit_factor: float = 1.3
    accept_max_drawdown: float = 0.15
    accept_windows: int = 6
    # Sharpe dégonflé : probabilité minimale que l'avantage résiste au nombre d'essais.
    accept_min_deflated_sharpe: float = 0.95
    research_trials: int = 41   # configurations essayées au total (à tenir à jour ; 2026-11-19 : Markov ; 2026-11-22 : Markov backtests, volume_expansion_bear, 3 candidates Luna ; 2026-11-24 : 2 pistes volume_expansion, puis chandelier, 4 h, RSI 2)
    notify_macos: bool = True
    # Alertes importantes par e-mail (Gmail : mot de passe d'application dans .env).
    notify_email_to: str | None = None
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    notify_email_max_per_day: int = 20
    notify_daily_summary: bool = True

    def validate_safety(self):
        if self.app_env != "paper":
            raise ValueError("Only paper mode is supported by this version.")
        from trading_agent.strategies import get_strategy
        if get_strategy(self.strategy).backtest_only:
            raise ValueError("Cette stratégie est réservée au backtest : elle ne peut pas piloter l'essai.")
        if not 0 < self.risk_per_trade_pct <= 0.01:
            raise ValueError("RISK_PER_TRADE_PCT must be in (0, 1%].")
        if not 0 < self.max_daily_loss_pct <= 0.02:
            raise ValueError("MAX_DAILY_LOSS_PCT must be in (0, 2%].")
        if not 0 < self.max_weekly_loss_pct <= 0.05:
            raise ValueError("MAX_WEEKLY_LOSS_PCT must be in (0, 5%].")
        if not 0 < self.max_total_exposure_pct <= 0.60:
            raise ValueError("MAX_TOTAL_EXPOSURE_PCT must be in (0, 60%].")
