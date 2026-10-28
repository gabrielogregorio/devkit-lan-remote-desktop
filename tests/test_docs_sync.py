"""
Documentation anti-drift guard.

A doc describing code that no longer exists is worse than no doc at all: the
next agent (or the author three months from now) believes it. These tests
extract from the CODE the list of routes, message types, presets and `LRD_*`
variables, and require every item to show up in the documentation.

They do not check wording - they check existence. When one fails, it is almost
always the doc that fell behind, not the test.
"""
import ast
import re
from pathlib import Path

import pytest

from server import audio, video
from server import main as M

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
README_MD = (ROOT / "README.md").read_text(encoding="utf-8")
TESTING_MD = (ROOT / "TESTING.md").read_text(encoding="utf-8")
ANTIBAN_MD = (ROOT / "ANTIBAN.md").read_text(encoding="utf-8")
ALL_DOCS = CLAUDE_MD + README_MD + TESTING_MD + ANTIBAN_MD


def _source(*parts) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


# ───────────────────────── HTTP/WS routes ─────────────────────────

def registered_routes() -> set[str]:
    """Extracts the paths of `app.router.add_*(...)` from build_app()."""
    tree = ast.parse(_source("server", "main.py"))
    routes = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith("add_") and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            routes.add(node.args[0].value)
    return routes


def test_every_route_is_documented():
    routes = registered_routes()
    assert routes, "found no route at all - did the parser break?"
    missing = [r for r in routes
               if r not in ("/", "/api/gamepad-map")  # described in prose
               and r not in CLAUDE_MD]
    assert not missing, f"routes without documentation in CLAUDE.md: {missing}"
    # the two special-cased ones must show up as well
    assert "/api/gamepad-map" in CLAUDE_MD
    assert "/api/monitors" in CLAUDE_MD


def test_documented_routes_actually_exist():
    """The reverse: the doc must not promise an endpoint that does not exist."""
    routes = registered_routes()
    promised = set(re.findall(r"`(/(?:api|ws)/[a-z\-/]+)`", CLAUDE_MD))
    nonexistent = [r for r in promised if r not in routes]
    assert not nonexistent, f"CLAUDE.md promises a route that does not exist: {nonexistent}"


def test_documented_debug_page_exists():
    assert (ROOT / "web" / "gamepad-debug.html").exists()
    assert "gamepad-debug.html" in CLAUDE_MD
    assert "gamepad-debug.html" in README_MD


# ───────────────────── /ws/input message types ─────────────────────

def handled_types() -> set[str]:
    """Accepted types: the ones in the ws_input dispatch + the ones in Injector.handle."""
    source = _source("server", "main.py") + _source("server", "inject.py")
    return set(re.findall(r'[t]\s*==\s*"(\w+)"', source))


def test_every_message_type_is_documented():
    missing = [t for t in handled_types()
               if f'"t":"{t}"' not in CLAUDE_MD.replace(" ", "")]
    assert not missing, (
        f"types accepted by the server and missing from the protocol in CLAUDE.md: "
        f"{sorted(missing)}")


def test_doc_does_not_promise_a_type_the_server_ignores():
    block = CLAUDE_MD.split("`/ws/input`")[1].split("## ")[0]
    documented = set(re.findall(r'\{"t"\s*:\s*"(\w+)"', block))
    assert documented, "could not find the documented protocol"
    ghosts = documented - handled_types()
    assert not ghosts, f"CLAUDE.md documents a nonexistent type: {sorted(ghosts)}"


def test_client_only_sends_types_the_server_handles():
    """The end-to-end side of the contract: what app.js sends, the server understands."""
    sent = set(re.findall(r'sendInput\(\{\s*t:\s*"(\w+)"', _source("web", "app.js")))
    assert sent, "could not find sendInput in the client"
    assert sent <= handled_types(), (
        f"client sends a type the server ignores: "
        f"{sorted(sent - handled_types())}")


# ───────────────────────── presets and ranges ─────────────────────────

@pytest.mark.parametrize("preset", list(video.PRESETS))
def test_video_preset_is_documented(preset):
    assert preset in CLAUDE_MD, f"video preset '{preset}' not mentioned in CLAUDE.md"
    assert preset in README_MD, f"video preset '{preset}' not mentioned in the README"


@pytest.mark.parametrize("preset", list(audio.AUDIO_PRESETS))
def test_audio_preset_is_documented(preset):
    assert preset in ALL_DOCS, f"audio preset '{preset}' not documented"


def test_readme_table_matches_the_real_presets():
    """Every row of the quality table has to reflect the actual PRESETS."""
    for name, p in video.PRESETS.items():
        row = next((line for line in README_MD.splitlines()
                    if line.strip().startswith(f"| {name} ")), None)
        assert row, f"preset '{name}' has no row in the README table"
        assert f"{p.max_h}p" in row, f"wrong resolution in the row for '{name}': {row}"
        assert f"| {p.fps} " in row, f"wrong fps in the row for '{name}': {row}"


def test_audio_for_quality_map_is_valid():
    """AUDIO_FOR_QUALITY has to match real presets on both sides."""
    assert set(M.AUDIO_FOR_QUALITY) == set(video.PRESETS)
    assert set(M.AUDIO_FOR_QUALITY.values()) <= set(audio.AUDIO_PRESETS)


def test_sanitizer_ranges_are_documented():
    """The 'vq' ranges are in the doc with the numbers from the code."""
    assert f"{video.RES_MIN}" in CLAUDE_MD and f"{video.RES_MAX}" in CLAUDE_MD
    assert f"{video.FPS_MIN}" in CLAUDE_MD and f"{video.FPS_MAX}" in CLAUDE_MD


def test_options_offered_by_the_api_are_accepted_by_the_sanitizer():
    """Internal consistency: the UI cannot offer a value the server rejects."""
    for r in [360, 480, 540, 720, 900, 1080]:
        assert video.clamp_res(r, -1) == r
    for f in [30, 45, 60, 90, 120]:
        assert video.clamp_fps(f, -1) == f
    for b in ["1200k", "2500k", "3500k", "7M", "12M", "25M"]:
        assert video.clean_bitrate(b, "!") == b


# ───────────────────────── environment variables ─────────────────────────

def used_envs() -> set[str]:
    sources = "".join(_source("server", f.name) for f in (ROOT / "server").glob("*.py"))
    sources += _source("run.sh")
    return set(re.findall(r"\bLRD_[A-Z_]+\b", sources))


def test_every_lrd_variable_is_documented():
    missing = sorted(v for v in used_envs() if v not in ALL_DOCS)
    assert not missing, f"variables used in the code and not documented: {missing}"


def test_claude_md_documents_the_server_variables():
    """The ones that change runtime behavior have to be in the agent guide."""
    missing = sorted(v for v in used_envs() if v not in CLAUDE_MD)
    assert not missing, f"missing from the CLAUDE.md table: {missing}"


def test_doc_does_not_invent_a_variable():
    documented = set(re.findall(r"\bLRD_[A-Z_]+\b", CLAUDE_MD + README_MD))
    ghosts = sorted(documented - used_envs())
    assert not ghosts, f"doc mentions a variable the code does not read: {ghosts}"


# ───────────────────────── file structure ─────────────────────────

@pytest.mark.parametrize("doc", ["CLAUDE.md", "README.md", "TESTING.md", "ANTIBAN.md"])
def test_docs_exist_and_are_not_empty(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert len(text) > 500, f"{doc} is empty or too shallow"


def test_claude_md_layout_mentions_every_server_module():
    for py in (ROOT / "server").glob("*.py"):
        if py.name == "__init__.py":
            continue
        assert py.name in CLAUDE_MD, f"module {py.name} missing from the layout in CLAUDE.md"


def test_every_test_file_is_in_testing_md():
    files = [p.name for p in (ROOT / "tests").glob("test_*.py")]
    files += [p.name for p in (ROOT / "web" / "__tests__").glob("*.test.js")]
    files += [p.name for p in (ROOT / "web" / "e2e").glob("*.spec.js")]
    missing = sorted(a for a in files if a not in TESTING_MD)
    assert not missing, f"test files not described in TESTING.md: {missing}"


def test_readme_test_commands_match_package_json():
    import json
    scripts = json.loads(_source("package.json"))["scripts"]
    for name in scripts:
        assert f"npm run {name}" in ALL_DOCS, f"script '{name}' not documented"


def test_internal_links_point_to_existing_files():
    for doc in ("CLAUDE.md", "README.md", "TESTING.md", "ANTIBAN.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        for target in re.findall(r"\]\((?!https?:)([^)#]+)\)", text):
            assert (ROOT / target).exists(), f"{doc} points to '{target}', which does not exist"
