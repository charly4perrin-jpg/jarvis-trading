"""Portefeuille réel en lecture seule : trois comportements essentiels, plus l'isolation."""
import asyncio
import json
from pathlib import Path

import pytest

from trading_agent import holdings
from trading_agent.demo_run import REPO

NOW = 1_800_000_000_000
FRESH = NOW - 60_000


def test_btc_sur_deux_plateformes_reste_deux_lignes(tmp_path):
    data = {"lines": {}, "statements": {}}
    holdings.set_line(data, "bitvavo", "BTC", 0.1, "2026-11-22", "test")
    holdings.set_line(data, "kraken", "btc", 0.05, "2026-11-20", "test")
    v = holdings.valuate(data, {"bitvavo:BTC": (80_000.0, FRESH), "kraken:BTC": (80_100.0, FRESH)}, NOW)
    assert [(l["exchange"], l["quantity"]) for l in v["lines"]] == [("bitvavo", 0.1), ("kraken", 0.05)]
    assert v["complete"] and v["total_eur"] == pytest.approx(8_000 + 4_005)
    # Chaque ligne garde sa propre date de relevé, distincte de la date du prix.
    assert {l["as_of"] for l in v["lines"]} == {"2026-11-22", "2026-11-20"}
    assert "Total : " in holdings.render(v)


@pytest.mark.parametrize("prices", [{}, {"kraken:ETH": (3_000.0, NOW - 3_600_000)},
                                    {"kraken:ETH": (float("nan"), FRESH)}])
def test_prix_manquant_ou_perime(prices):
    data = {"lines": {}, "statements": {}}
    holdings.set_line(data, "bitvavo", "BTC", 0.1, "2026-11-22", "test")
    holdings.set_line(data, "kraken", "ETH", 2, "2026-11-22", "test")
    v = holdings.valuate(data, {"bitvavo:BTC": (80_000.0, FRESH), **prices}, NOW)
    eth = next(l for l in v["lines"] if l["asset"] == "ETH")
    assert eth["status"] == holdings.UNAVAILABLE and eth["value_eur"] is None
    assert not v["complete"] and v["missing"] == 1
    text = holdings.render(v)
    assert "Total partiel" in text and "PAS la valeur totale" in text and "Total : " not in text


def test_meme_releve_importe_deux_fois(tmp_path):
    csv = tmp_path / "releve.csv"
    csv.write_text("plateforme;actif;quantite;date\nbitvavo;BTC;0,1;2026-11-21\nkraken;ETH;2;2026-11-21\n")
    paper = REPO / "data" / "paper_state.json"
    events = REPO / "data" / "events.jsonl"
    before = [p.stat().st_mtime_ns if p.exists() else None for p in (paper, events)]
    store = tmp_path / "holdings.json"
    first = holdings.import_csv(csv, store)
    second = holdings.import_csv(csv, store)
    assert not first["already_imported"] and second["already_imported"]
    lines = holdings.load(store)["lines"]
    assert {k: l["quantity"] for k, l in lines.items()} == {"bitvavo:BTC": 0.1, "kraken:ETH": 2.0}
    after = [p.stat().st_mtime_ns if p.exists() else None for p in (paper, events)]
    assert before == after          # portefeuille papier et journal inchangés


def test_refuse_d_ecrire_dans_l_essai_papier():
    with pytest.raises(ValueError, match="essai papier"):
        holdings.save({"lines": {}, "statements": {}}, REPO / "data" / "paper_state.json")


def test_quantite_zero_retire_la_ligne_et_quantite_invalide_refusee():
    data = {"lines": {}, "statements": {}}
    holdings.set_line(data, "bitvavo", "BTC", 0.1, "d", "t")
    holdings.set_line(data, "bitvavo", "BTC", 0, "d", "t")
    assert data["lines"] == {}
    for bad in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            holdings.set_line(data, "bitvavo", "BTC", bad, "d", "t")


def test_scenarios_n_ecrivent_pas_dans_l_essai(monkeypatch):
    """Les scénarios extrêmes restent dans leur dossier temporaire (plus de data/shadow/sim.json)."""
    from trading_agent import scenarios
    shadow = REPO / "data" / "shadow" / "sim.json"
    before = shadow.stat().st_mtime_ns if shadow.exists() else None
    report = asyncio.run(scenarios.run_all())
    assert report["passed"] == report["total"]
    assert (shadow.stat().st_mtime_ns if shadow.exists() else None) == before


def test_comparaison_avec_l_essai_papier_sans_le_modifier(tmp_path):
    state = tmp_path / "paper_state.json"
    state.write_text(json.dumps({"cash": 2900.0, "equity": 3010.0,
                                 "positions": {"BTC/EUR": {"quantity": 0.002}}}))
    before = state.read_bytes()
    text = holdings.render_paper({"essai": holdings.paper_summary(state),
                                  "absent": holdings.paper_summary(tmp_path / "nope.json")})
    assert "capital 3 010,00 €" in text and "0.002 BTC/EUR" in text and "absent" not in text
    assert state.read_bytes() == before


def test_interface_affiche_le_vrai_portefeuille_avec_cache(tmp_path, monkeypatch):
    import time
    from tests.test_server import make_client
    store = tmp_path / "holdings.json"
    data = {"lines": {}, "statements": {}}
    holdings.set_line(data, "bitvavo", "BTC", 0.1, "2026-11-22", "test")
    holdings.save(data, store)
    monkeypatch.setattr(holdings, "DEFAULT_PATH", str(store))
    calls = []

    async def fake_prices(d, now_ms=None):
        calls.append(1)
        now = int(time.time() * 1000)
        return {"bitvavo:BTC": (80_000.0, now)}, now
    monkeypatch.setattr(holdings, "fetch_prices", fake_prices)
    client = make_client(tmp_path)
    first = client.get("/api/holdings").json()
    second = client.get("/api/holdings").json()
    assert first["complete"] and first["total_eur"] == pytest.approx(8_000)
    assert second["lines"][0]["as_of"] == "2026-11-22" and len(calls) == 1   # cours gardés 60 s
