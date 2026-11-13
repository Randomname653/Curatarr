"""The admin views in the user's language, round three (UX design record 3.8).

* Drawn in German, a deletion card (Cut / Your call, Why?, Keep, Discuss,
  Delete, Fix match, the menu) says German words - and the same actions,
  arguments, data attributes and titles as in English.
* The curator's map of the app names the admin buttons in the user's UI
  language; its English is the one the curator always read.
* Server words stay as they came: a proposal's pitch and the service name
  pass through untouched in both languages.

    python tests/test_i18n_round_c.py
"""
from _harness import check  # noqa: I001 - first: temp stores
from _i18n_js import i18n_js

import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
node = shutil.which("node")

STUBS = r"""
const document = {getElementById: () => null, createElement: () => ({}), addEventListener() {}};
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const escAttr = esc;
const act = (name, ...args) => `data-action="${name}"` + (args.length ? ` data-args='${JSON.stringify(args)}'` : '');
const actOn = (ev, name, ...args) => `data-on-${ev}="${name}"` + (args.length ? ` data-args='${JSON.stringify(args)}'` : '');
const EL = '$el', EVENT = '$event';
const SVG_CHECK = '<svg width="13" height="13"></svg>', SVG_TRASH = '', SVG_WARN = '<svg width="13" height="13"></svg>';
const _posterImg = () => '<img>';
const menuHtml = items => items.filter(Boolean).map(i =>
  `<button ${i.action || `href="${i.href}"`} ${i.attrs || ''} title="${esc(i.title || '')}">${esc(i.label)}</button>`).join('');
const haptic = () => {}, setPulse = () => {}, toast = () => {}, btnBusy = () => {}, btnDone = () => {};
const _errHtml = () => '', _errMsg = e => String(e), _fmtAbs = () => '', _fmtRel = () => '', emptyHtml = () => '';
const confirmDialog = async () => ({ok: false});
const state = {}; const openMatchPicker = () => {}; const api = async () => ({});
const _busyStreaming = () => false, enterThread = async () => false, proposalOpener = () => '', sendMessage = () => {};
const tabToHash = () => '';
"""

PROPOSALS = [
    {"id": 7, "title": "The Lantern Room", "category": "movie", "service": "radarr", "media_id": 412,
     "size_gb": 31.5, "stagnant": True, "pitch": "Unwatched for three years.", "genres": "Mystery, Drama",
     "arr_url": "http://radarr.local/movie/412",
     "score_factors": {"total": 46, "guard": "household_floor",
                       "factors": [{"label": "taste mismatch", "points": 32}, {"label": "size", "points": 14}]}},
    {"id": 8, "title": "Gull Point", "category": "show", "service": "plex", "size_gb": 8,
     "pitch": "Dropped after one episode.", "user_comment": "too slow"},
]


def render(lang):
    body = (ROOT / "frontend/js/deletions.js").read_text(encoding="utf-8")
    body = re.sub(r"^import .*$", "", body, flags=re.M)
    body = body.replace("export async function", "async function").replace("export function", "function") \
               .replace("export const", "const").replace("export let", "let")
    js = i18n_js(lang) + STUBS + body + """
const ps = %s;
console.log(JSON.stringify({cards: ps.map(p => _delCardHtml(p) + _whyPanel(p)).join('\\n')}));
""" % json.dumps(PROPOSALS)
    r = subprocess.run([node, "-"], input=js, capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        print(r.stderr[:1200])
        return {}
    return json.loads(r.stdout.strip().splitlines()[-1])


def _ids(html):
    return (re.findall(r'data-(?:action|on-\w+)="(\w+)"', html), re.findall(r"data-args='([^']*)'", html),
            re.findall(r'data-(?:pid|id|del-id|service|mediaid|category|title|pitch)="([^"]*)"', html),
            re.findall(r"<b>([^<]*)</b>", html), re.findall(r'href="([^"]*)"', html))


if node:
    en, de = render("en"), render("de")
    check("the deletion cards render in both languages", bool(en.get("cards") and de.get("cards")))
    words = ("Deine Entscheidung", "Streichen", "Warum?", "Behalten", "Besprechen", "Löschen",
             "Zuordnung korrigieren")
    check("in German: Cut / Your call, Why?, Keep, Discuss, Delete, Fix match",
          all(w in de.get("cards", "") for w in words), [w for w in words if w not in de.get("cards", "")])
    check("...and none of those English labels is left",
          not re.search(r">(Your call|Cut|Why\?|Keep|Discuss|Delete|Fix match)<", de.get("cards", "")))
    check("...the same actions, arguments, data attributes, titles and links as in English",
          en.get("cards") and _ids(en["cards"]) == _ids(de["cards"]), (_ids(en.get("cards", ""))[0], _ids(de.get("cards", ""))[0]))
    check("...the pitch, the service and the curator's signal labels pass through as the server sent them",
          all(s in de.get("cards", "") for s in ("Unwatched for three years.", "radarr", "taste mismatch", "too slow")))
else:
    print("  (node not installed - the German rigs are skipped)")

# ── the curator names the admin buttons the user sees ───────────────────────
from src.services import app_context  # noqa: E402

de_map = app_context.render(app_context.APP_MAP_TEMPLATE, "de")
en_map = app_context.render(app_context.APP_MAP_TEMPLATE, "en")
check("the app map in German names the admin buttons as the German UI draws them",
      all(f"'{w}'" in de_map for w in ("Deine Entscheidung", "Streichen", "Warum?", "Löschen", "Dauerhaft schützen",
                                         "Ausgewählte löschen", "Zuordnung korrigieren", "Geschützte Titel",
                                         "Kandidaten zum Verkleinern", "Gelernte Grundsätze")))
check("...the English one is the map the curator always read",
      en_map == app_context.APP_MAP_BLOCK and "'Your call'" in en_map and "'Protect permanently'" in en_map
      and "⟦" not in en_map + de_map)
de_kb = app_context.render(app_context.KB_HEALING_TEMPLATE, "de")
check("...the Knowledge Base's healing actions too",
      all(f"'{w}'" in de_kb for w in ("Suchen & festlegen", "Jetzt neu versuchen", "Befund verwerfen")))
de_disc = app_context.render(app_context.DISCUSSION_UI_TEMPLATE, "de")
check("...and the deletion discussion's 'Delete & exit' / 'Exit discussion'",
      "'Löschen & beenden'" in de_disc and "'Diskussion beenden'" in de_disc)
check.done()
