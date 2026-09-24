import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # the cua package
sys.path.insert(0, str(Path(__file__).resolve().parent))           # fakes


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Runs, maps and capabilities go to a temp folder; no beeps, no recording of a person."""
    from cua import settings
    from cua.engine import hooks, llm
    monkeypatch.setattr(settings, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(settings, "MAPS", tmp_path / "maps")
    monkeypatch.setattr(settings, "CAPS", tmp_path / "capabilities")
    monkeypatch.setattr(settings, "RECORD_HUMAN", False)
    monkeypatch.setattr(hooks, "alert", lambda: None)
    monkeypatch.setattr(llm, "client", None)
    return tmp_path
