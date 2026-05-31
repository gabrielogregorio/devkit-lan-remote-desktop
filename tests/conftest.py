import os
import sys

# DEVE rodar ANTES de importar qualquer server.* ---
os.environ["RL_GUARDIAN"] = "off"
os.environ["RL_ENCODER"] = "x264"
os.environ.setdefault("RL_AUDIO_PRESET", "media")

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("RL_GUARDIAN", "off")
    monkeypatch.setenv("RL_ENCODER", "x264")
    yield
