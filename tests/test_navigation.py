"""Navigation holds together: every sidebar entry is a real link to a real
view, every view can take focus, the hash router is wired, and moving
markup around left no duplicate ids.

2026-10 UI audit: the sidebar was div role=button with no aria-current and
no URL (nothing could be bookmarked, Back left the app, focus stayed in the
sidebar after a click); Users and Libraries existed twice, as a sidebar view
and as a Settings pane, and the copies had drifted (only the view had the
Spotify import). This suite pins the new shape. A browser run of the router
itself is not part of the battery (no browser in CI); the checks here are
the structure that run relies on.

    python tests/test_navigation.py
"""
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.frontend_files import markup  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NAV = (ROOT / "frontend/js/nav.js").read_text(encoding="utf-8")
APP = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
AUTH = (ROOT / "frontend/js/auth.js").read_text(encoding="utf-8")
SETTINGS = (ROOT / "frontend/js/settings.js").read_text(encoding="utf-8")


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def parse(html):
    t = Tags()
    t.feed(html)
    return t.tags


def sidebar_html(m):
    return m[m.index('<nav id="sidebar">'):m.index('</nav>', m.index('<nav id="sidebar">'))]


def test_sidebar_entries_are_links_to_existing_views():
    m = markup()
    tags = parse(m)
    view_ids = {a["id"][:-5] for t, a in tags if "id" in a and a.get("class", "").split()[:1] == ["view"]}
    items = [(t, a) for t, a in parse(sidebar_html(m)) if "sb-item" in a.get("class", "").split()]
    assert items, "no sidebar entries found"
    for tag, a in items:
        assert tag == "a", f"sidebar entry is a <{tag}>, not a link: {a.get('data-view')}"
        assert "role" not in a, f"a link needs no role: {a.get('data-view')}"
        assert a.get("href") == "#" + a.get("data-view", "?"), f"href and data-view disagree: {a}"
        assert a["data-view"] in view_ids, f"sidebar links to a view that does not exist: {a['data-view']}"
    print(f"  {len(items)} sidebar links, each to an existing view")


def test_admin_views_sit_in_the_admin_section_and_the_guard():
    side = sidebar_html(markup())
    curate = side.split('<div class="sb-section admin-action-row" hidden>')[1].split('<div class="sb-section">')[0]
    in_section = set(re.findall(r'data-view="([\w-]+)"', curate))
    guarded = set(re.findall(r"'([\w-]+)'", NAV.split("const ADMIN_VIEWS = [")[1].split("]")[0]))
    assert in_section == guarded == {"deletions", "curation", "reclassify", "report"}, (in_section, guarded)
    assert "history.replaceState(null, '', '#chat')" in NAV, "a non-admin on an admin hash must land on Chat"


def test_every_view_heading_can_take_focus():
    tags = parse(markup())
    m = markup()
    for vid in re.findall(r'<div class="view[^"]*" id="([\w-]+-view)"', m):
        block = m[m.index(f'id="{vid}"'):]
        h1 = re.search(r"<h1([^>]*)>", block)
        assert h1 and 'tabindex="-1"' in h1.group(1), f"{vid}: its first heading cannot take focus"
    assert 'class="sr-only" tabindex="-1">Chat</h1>' in m, "Chat needs a (visually hidden) heading"
    assert "querySelector('h1')?.focus(" in NAV, "showView no longer moves focus to the heading"
    assert "setAttribute('aria-current', 'page')" in NAV, "the active entry must carry aria-current"
    assert len(tags) > 100


def test_router_is_wired():
    assert "window.addEventListener('hashchange', () => routeFromHash())" in APP
    assert "routeFromHash({focus: false})" in AUTH, "sign-in must open the bookmarked view, without stealing focus"
    assert "history.pushState(null, '', '#' + name)" in NAV
    assert "history.replaceState(null, '', '#settings/' + name)" in SETTINGS


def test_settings_tabs_match_panes_and_no_duplicate_views():
    m = markup()
    tabs = set(re.findall(r'class="settings-tab[^"]*"[^>]*data-pane="([\w-]+)"', m))
    panes = set(re.findall(r'<section class="settings-pane" data-pane="([\w-]+)"', m))
    assert tabs == panes, (tabs, panes)
    assert {"plex-libraries", "users", "library", "maintenance"} <= panes
    assert 'id="libraries-view"' not in m and 'id="admin-view"' not in m, "a second copy of a Settings pane is back"
    users = m.split('data-pane="users" hidden>')[1].split("</section>\n\n")[0]
    assert 'id="adm-sp-zone"' in users, "the Spotify import belongs to Settings → Users"
    assert 'data-action="shutdownServer"' in m.split('<section class="settings-pane" data-pane="maintenance"')[1].split('<section class="settings-pane"')[0], "Stop server lives in Maintenance"
    assert 'data-action="shutdownServer"' not in sidebar_html(m), "Stop server must not sit in the nav"


def test_no_duplicate_ids():
    ids = [a["id"] for _, a in parse(markup()) if "id" in a]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate ids in index.html: {dupes}"


def test_no_div_buttons_left_in_chrome():
    m = markup()
    assert '<button type="button" id="user-pill"' in m, "the user pill is a real button"
    assert 'role="button"' not in sidebar_html(m)


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
