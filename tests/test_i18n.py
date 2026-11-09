"""The UI in the user's language (UX design record, stage 3.8).

* The dictionaries (frontend/i18n/en.json, de.json) hold the same keys, the
  same {values} and the same plural forms.
* Markup that carries data-i18n says the English of en.json; every key a
  module asks for (t('…') / th('…')) is in the dictionary.
* The setting (Settings → Account, users.ui_language) beats the detected
  chat language; "Automatic" is the detected one. It decides the UI and the
  server's own texts, never the language of a chat answer.
* The server reads the same dictionary: the curator's app map names the UI
  in the user's language, the bell's labels and the chat's status and busy
  lines follow it.
* German changes words, never technical identifiers: actions, their
  arguments, categories, lanes and links are the same in both languages.

    python tests/test_i18n.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores
from _i18n_js import dictionary, i18n_js

import asyncio
import html
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
EN, DE = dictionary("en"), dictionary("de")
_VARS = re.compile(r"\{(\w+)\}")


def _forms(v):
    return v if isinstance(v, dict) else {"": v}


# ── the dictionaries ────────────────────────────────────────────────────────

check("English and German hold the same keys", set(EN) == set(DE),
      (sorted(set(EN) - set(DE))[:10], sorted(set(DE) - set(EN))[:10]))
check("...with the same plural forms", all(set(_forms(EN[k])) == set(_forms(DE[k])) for k in EN if k in DE),
      [k for k in EN if k in DE and set(_forms(EN[k])) != set(_forms(DE[k]))])
mismatch = [k for k in EN if k in DE and any(
    set(_VARS.findall(e)) != set(_VARS.findall(_forms(DE[k]).get(f, ""))) for f, e in _forms(EN[k]).items())]
check("...and the same {values} in every text", not mismatch, mismatch)
# German word order can leave the lead or the tail of a sentence empty.
_MAY_BE_EMPTY = {"recs.add_question_pre", "pwa.ios_step1_post"}
check("...no German text is empty where the English is not (a sentence's lead or tail may be)",
      [k for k in EN if _forms(EN[k]).get("") and not _forms(DE[k]).get("") and k not in _MAY_BE_EMPTY] == [])

# ── the markup and the modules ──────────────────────────────────────────────

markup = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
text_marks = re.findall(r'data-i18n="([\w.]+)"[^>]*>([^<]*)<', markup)
attr_marks = re.findall(r'\b(title|aria-label|placeholder)="([^"]*)" data-i18n-\1="([\w.]+)"', markup)
bad = [(k, txt) for k, txt in text_marks if html.unescape(" ".join(txt.split())) != EN.get(k)]
bad += [(k, v) for _a, v, k in attr_marks if html.unescape(v) != EN.get(k)]
check("markup with a key says that key's English: the page reads right before the first switch",
      text_marks and attr_marks and not bad, bad[:8])
check("...and every key the markup names is in the dictionary",
      all(k in EN for k in re.findall(r'data-i18n(?:-[\w-]+)?="([\w.]+)"', markup)))
js = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "frontend" / "js").glob("*.js"))
asked = set(re.findall(r"\bth?\(\s*'([\w.]+)'", js))
check("every key a module asks for by name is in the dictionary", asked and asked <= set(EN),
      sorted(asked - set(EN)))
prefixes = set(re.findall(r"\bth?\(\s*`([\w.]+)\.\$\{", js))
check("...and every key family it builds (`recs.lane.${lane}.title`) has members",
      all(any(k.startswith(p + ".") for k in EN) for p in prefixes), prefixes)

# ── the setting beats detection; it never sets the answer's language ────────

conn = app_db()
from src.database.models import ConversationMessage, User  # noqa: E402
from src.services import user_language as ul  # noqa: E402

with conn.get_db_session() as db:
    u = User(plex_user_id="i18n-1", plex_username="Member")
    db.add(u)
    db.commit()
    UID = u.id
    for text in ("Was soll ich heute Abend schauen? Ich habe Lust auf etwas Ruhiges.",
                 "Ich mag die Serie, aber das Ende war nicht so gut.", "Und was ist mit dem Film von gestern?"):
        db.add(ConversationMessage(user_id=UID, role="user", content=text, thread_id="general"))
    db.commit()

check("Automatic: the language the user writes to the curator in",
      ul.stored_setting(UID) == "auto" and ul.user_language(UID) == "de" and ul.ui_language(UID) == "de")
ul.set_setting(UID, "en")
check("English picked: the UI is English, though the chats are German",
      ul.user_language(UID) == "en" and ul.user_locale(UID) == "en-US" and ul.detected_language(UID) == "de")
ul.set_setting(UID, "auto")
check("...back to Automatic: German again", ul.user_language(UID) == "de")
try:
    ul.set_setting(UID, "fr")
    check("a language the UI is not written in is refused", False)
except ValueError:
    check("a language the UI is not written in is refused", True)

from src.routers import auth as auth_router  # noqa: E402
from src.routers import users as users_router  # noqa: E402

me = SimpleNamespace(id=UID, is_admin=False, plex_username="Member", timezone=None)
picked = users_router.set_ui_language(users_router.UiLanguageUpdate(language="en"), user=me)
shown = users_router.get_ui_language(user=me)
check("the Account route stores the pick and answers the language and its locale",
      picked == {"setting": "en", "language": "en", "locale": "en-US"}
      and shown == {"setting": "en", "detected": "de", "language": "en", "choices": ["auto", "en", "de"]}, shown)
check("/api/auth/status tells the UI which language to draw", auth_router.auth_status(user=me)["ui_language"] == "en")
try:
    users_router.set_ui_language(users_router.UiLanguageUpdate(language="xx"), user=me)
    check("a bad value is a 400", False)
except Exception as e:  # noqa: BLE001 - FastAPI's HTTPException
    check("a bad value is a 400", getattr(e, "status_code", None) == 400)
ul.set_setting(UID, "auto")

# ── the server reads the same words ─────────────────────────────────────────

from src.services import app_context, chat_prompt, i18n, llm_lane  # noqa: E402
from src.services.proactive_messages import trigger_label  # noqa: E402

check("the server's t(): plural forms, values, English as the fallback, the key as the last resort",
      i18n.t("chat.status.memories", "de", n=1) == "1 passende Erinnerung geladen"
      and i18n.t("chat.status.memories", "en", n=3) == "3 relevant memories loaded"
      and i18n.t("recs.year_ago.head", "fr") == "A year ago" and i18n.t("no.such.key", "de") == "no.such.key")
de_map = app_context.render(app_context.APP_MAP_TEMPLATE, "de")
check("the curator's app map names the UI the user sees, in German, around English prose",
      "'Empfehlungen'" in de_map and "'Löschvorschläge' (admin)" in de_map and "'Bibliothek durchsuchen…'" in de_map
      and "APP MAP (Curatarr's own UI" in de_map and "'Recommendations'" not in de_map)
de_prompt = chat_prompt.build_system_prompt(voice="neutral", user_lang="en", ui_lang="de")
en_prompt = chat_prompt.build_system_prompt(voice="neutral", user_lang="en")
check("a chat in English for a German UI: the answer rule says English, the map says the German buttons",
      "'Empfehlungen'" in de_prompt and "'Recommendations'" in en_prompt
      and de_prompt.split("CRITICAL BEHAVIOR RULES:")[1].split("\n")[1]
      == en_prompt.split("CRITICAL BEHAVIOR RULES:")[1].split("\n")[1])
check("the bell's labels in the user's language", trigger_label("last_night", "de") == "Morgenzeile"
      and trigger_label("last_night") == "Morning line" and trigger_label("protection_intent", "de") == "Behalten-Wunsch")
check("the GPU-busy notice in German, its technical reason as it is",
      llm_lane.busy_message("17544/24564 MB, 92 %", lane_open=True, lang="de").startswith("Die Grafikkarte ist gerade")
      and "17544/24564 MB, 92 %" in llm_lane.busy_message("17544/24564 MB, 92 %", lane_open=True, lang="de"))

# ── German changes words, never identifiers ─────────────────────────────────

node = shutil.which("node")
if node:
    recs = (ROOT / "frontend" / "js" / "recs.js").read_text(encoding="utf-8")
    chat = (ROOT / "frontend" / "js" / "chat.js").read_text(encoding="utf-8")

    def fn(src, name):
        m = re.search(r"^(?:export )?(?:async )?function %s\(" % re.escape(name), src, re.M)
        end = re.compile(r"^\}$", re.M).search(src, m.start())
        return src[m.start():end.end()].removeprefix("export ")

    def render(lang):
        body = i18n_js(lang) + r"""
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const escAttr = esc;
const act = (name, ...args) => `data-action="${name}"` + (args.length ? ` data-args='${JSON.stringify(args)}'` : '');
const actOn = () => '';
const EL = '$el', EVENT = '$event';
const _posterImg = () => '<img>';
const _whyButton = () => '';
const serverLabel = () => 'Plex';
const state = {currentUser: {is_admin: true}};
let _membersWishViaPlex = false;
const _LANES = ['library', 'discovery', 'because', 'rediscover'];
const _laneMeta = lane => (_LANES.includes(lane)
  ? { title: t(`recs.lane.${lane}.title`), sub: t(`recs.lane.${lane}.sub`) } : { title: lane, sub: '' });
""" + fn(recs, "_recWhy") + "\n" + fn(recs, "_recCard") + "\n" + fn(recs, "_recSection") + "\n" + fn(chat, "_staticChipsHtml") + """
const lib = {title: 'The Lantern Room', lane: 'library', category: 'movie', confidence: 0.82, link: 'https://app.plex.tv/x',
             reason: 'Close to what you watched.', genres: 'Mystery'};
const disc = {title: 'Gull Point', lane: 'discovery', category: 'show', year: 2021, reason: 'A coastal mystery.'};
console.log(JSON.stringify({recs: _recSection('library', [lib]) + _recSection('discovery', [disc]), chips: _staticChipsHtml()}));
"""
        r = subprocess.run([node, "-"], input=body, capture_output=True, text=True, encoding="utf-8")
        if r.returncode:
            print(r.stderr[:800])
        return json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}

    en, de = render("en"), render("de")
    check("the recommendations in German: lane, buttons and badges in German",
          all(w in de.get("recs", "") for w in ("Aus deiner Bibliothek", "In Plex öffnen", "Besprechen",
                                                "82 % passend", "Neu entdecken", "+ Hinzufügen")), de.get("recs", "")[:600])
    ids = lambda h: (re.findall(r'data-action="(\w+)"', h), re.findall(r"data-args='([^']*)'", h),  # noqa: E731
                     re.findall(r'href="([^"]*)"', h), re.findall(r'data-(?:category|title|year)="([^"]*)"', h))
    check("...the actions, their arguments, the links and the data the same as in English",
          en.get("recs") and ids(en["recs"]) == ids(de["recs"]), (ids(en.get("recs", "")), ids(de.get("recs", ""))))
    check("a German chip fills the German question and keeps its kind 'tonight'",
          "Was soll ich heute Abend schauen?" in de.get("chips", "")
          and re.findall(r'data-action="(\w+)"', de.get("chips", "")) == re.findall(r'data-action="(\w+)"', en.get("chips", ""))
          and '"tonight"]' in de.get("chips", ""), de.get("chips"))
else:
    print("  (node not installed - the German rendering is not run)")

check.done()
