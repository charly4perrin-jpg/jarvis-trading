"""Filet de sécurité : pendant les tests, tous les chemins de fichiers de la
configuration pointent vers un dossier temporaire. Aucun test ne peut écrire dans
les données de l'essai en cours (data/)."""
import pytest

from trading_agent.config import Settings


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    for name in Settings.model_fields:
        if name.endswith(("_path", "_dir")):
            monkeypatch.setenv(name.upper(), str(tmp_path / "isolé" / name))
    yield
