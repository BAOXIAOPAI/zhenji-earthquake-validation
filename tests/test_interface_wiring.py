"""The interface wiring: no dead element references, no routes that do not exist.

A `$('id')` that names an element the page never defines is invisible in review but
throws in the browser and makes a control unusable — the "清空" button was exactly
that before this batch. These checks are static on purpose: they need no browser and
catch the whole class of failure.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "main/static/app.js").read_text()
HTML = (ROOT / "main/static/index.html").read_text()
SERVER = (ROOT / "main/server.py").read_text()


def _ids_in_html():
    return set(re.findall(r'id="([^"]+)"', HTML))


def _ids_used_by_js():
    """Ids the script addresses, including ones passed as an argument.

    `$('x')` covers direct lookups; the quoted-token scan covers ids handed to a
    helper (`uShowEvidence(rid, 'u-evidence')`), which the narrower pattern misses.
    """
    direct = set(re.findall(r"\$\('([^']+)'\)", APP))
    quoted = set(re.findall(r"'([A-Za-z][A-Za-z0-9_-]*)'", APP))
    return direct | quoted


def _directly_addressed_by_js():
    return set(re.findall(r"\$\('([^']+)'\)", APP))


def test_every_element_the_script_reaches_for_exists_in_the_page():
    missing = sorted(_directly_addressed_by_js() - _ids_in_html())
    assert not missing, ("app.js reaches for elements the page does not define: "
                        f"{missing}")


def test_the_page_does_not_carry_elements_the_script_never_uses():
    """The reverse direction: a leftover id is how `u-par` rotted unnoticed."""
    unused = sorted(_ids_in_html() - _ids_used_by_js())
    # View containers are switched by class through the nav handler, and the two
    # cards are scroll anchors; neither is looked up by id.
    allowed = {"view-workbench", "view-records", "view-about",
               "conclusion-card", "feedback-card"}
    # Tab ids are referenced by the panels' accessibility relationships.
    for references in re.findall(r'aria-(?:labelledby|describedby)="([^"]+)"', HTML):
        allowed.update(references.split())
    assert not (set(unused) - allowed), f"unused element ids: {sorted(set(unused) - allowed)}"


def test_every_api_route_the_script_calls_is_served():
    routes = set(re.findall(r"api\('(/api/[a-z_/]*)", APP))
    # dynamic suffixes are built by concatenation; check the static prefix
    assert routes, "expected the script to call the API"
    for route in sorted(routes):
        stem = route.rstrip("/")
        assert stem in SERVER, f"app.js calls {route} but server.py never mentions it"


def test_the_new_controls_are_present_and_wired():
    """The three-view workbench's controls must exist *and* have a handler."""
    for el in ("btn-example", "btn-new", "u-question", "u-parse", "u-run",
               "wave-canvas", "tool-timeline", "compare-body",
               "fb-station", "fb-exclude", "fb-include", "fb-goal", "fb-goal-apply",
               "fb-grid", "fb-grid-apply", "fb-result", "fb-warn"):
        assert f'id="{el}"' in HTML, f"missing control: {el}"
    for handler in ("btn-example'", "btn-new'", "u-parse'", "u-run'",
                    "fb-exclude'", "fb-include'", "fb-goal-apply'",
                    "fb-grid-apply'"):
        assert f"$('{handler}).onclick" in APP, f"control has no handler: {handler}"


def test_the_views_are_reachable_by_deep_link():
    """Screens must be linkable so a reviewer can point at one.

    The workbench redesign uses query parameters (`?view=`, `?run=`) instead of
    the old hash tabs; the deep link must still select the requested view after
    the default example has loaded.
    """
    assert "URLSearchParams" in APP and "setView(" in APP
    assert "params.get('view')" in APP and "params.get('run')" in APP
    # A deep-linked view is applied AFTER the default workbench load, not before.
    init = APP.split('(async function init()', 1)[1]
    assert 'navigate: false' in init, "initial load must preserve the linked view"
    assert init.index('await loadRun(') < init.index("setView(view || 'workbench'"), \
        "deep-linked view must be applied after loadRun"
    for view in ("workbench", "records", "about"):
        assert f'data-view="{view}"' in HTML
