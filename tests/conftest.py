import os
import sys

# MUST run BEFORE importing any server.* ---
os.environ["LRD_GUARDIAN"] = "off"
os.environ["LRD_ENCODER"] = "x264"
os.environ.setdefault("LRD_AUDIO_PRESET", "medium")

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("LRD_GUARDIAN", "off")
    monkeypatch.setenv("LRD_ENCODER", "x264")
    yield
