"""
Curatarr — the wrap-up story: the fact sheet, the stand-in, the task.

``render_fact_sheet`` numbers what wrapup_facts decided — facts F#, titles
T#, owned-unwatched picks U#, clusters C# — and ends with the ranked insight
cards (services/wrapup_insights). The curator writes the story from those
cards in services/wrapup_writer (an editor picks, a writer tells, every
sentence checked against the cards it cites, repaired slide by slide). When
the curator cannot answer at all, ``fallback_story`` stands in, built from
the same facts, so the page never shows an error where a story should be.

The same fact sheet is the hidden context of an "Ask your wrap-up" chat
thread (routers/chat, kind "wrapup"), so the follow-up conversation stays
on the same verified numbers.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

PHASES = ("Counting plays", "Finding the outliers", "Mapping taste clusters",
          "Fetching artwork", "Curator is writing", "Checking the facts")

_NUM_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*")


def _norm_num(n: str) -> str:
    """'50,000' → '50000', '13,5' → '13.5', '120.0' → '120'."""
    if re.fullmatch(r"\d{1,3}(,\d{3})+", n):
        n = n.replace(",", "")
    n = n.replace(",", ".")
    if re.fullmatch(r"\d+\.0+", n):
        n = n.split(".")[0]
    return n


def numbers_in(text: str) -> set:
    return {_norm_num(n) for n in _NUM_RE.findall(text or "")}


def _pct(x) -> int:
    return round(100 * (x or 0))


# ── the fact sheet ──────────────────────────────────────────────────────────

def render_fact_sheet(f: dict) -> tuple:
    """(sheet text, index). The index maps every reference to what it stands
    for and holds the numbers the prose may use."""
    facts: list = []
    items: dict = {}
    picks: dict = {}
    clusters: dict = {}
    posters = f.get("posters") or {}

    def fact(text: str) -> str:
        fid = f"F{len(facts) + 1}"
        facts.append(f"{fid}: {text}")
        return fid

    def item(title: str, media_type: Optional[str] = None) -> str:
        for k, v in items.items():
            if v["title"] == title:
                if media_type and not v.get("media_type"):
                    v["media_type"] = media_type
                return k
        tid = f"T{len(items) + 1}"
        items[tid] = {"title": title, "media_type": media_type, "poster": posters.get(title)}
        return tid

    def pick(title: str, media_type: Optional[str], poster: Optional[str] = None,
             year=None, reason: Optional[str] = None) -> str:
        uid = f"U{len(picks) + 1}"
        picks[uid] = {"title": title, "media_type": media_type,
                      "poster": poster or posters.get(title), "year": year, "reason": reason}
        return uid

    p = f.get("period") or {}
    t = f.get("tempo") or {}
    fact(f"Period: {p.get('label', '')}. Scope: {f.get('scope', 'all')}.")
    if t:
        approx = "about " if t.get("hours_estimated_share", 0) > 0.25 else ""
        fact(f"{approx}{t['hours']} hours over {t['plays']} plays on {t['active_days']} "
             f"active days; longest daily streak {t['longest_streak_days']} days.")
        if t.get("prev_hours"):
            fact(f"Same span before: {t['prev_hours']} hours.")
        dp = t.get("dayparts") or {}
        fact(f"Peak hour {t['peak_hour']}:00; {_pct(dp.get('night'))}% at night, "
             f"{_pct(dp.get('evening'))}% in the evening, {_pct(t.get('weekend_share'))}% on weekends.")
        bd = t.get("biggest_day") or {}
        if bd:
            fact(f"Biggest day {bd['date']}: {bd['hours']} hours, {bd['plays']} plays.")
        if t.get("title_streak"):
            s = t["title_streak"]
            fact(f"Longest run with one title: {item(s['title'])} {s['title']} on {s['days']} days in a row.")
    for i, top in enumerate(f.get("top") or []):
        ref = item(top["title"], top["media_type"])
        fact(f"Top {i + 1}: {ref} {top['title']} ({top['media_type']}) — {top['hours']} hours, {top['plays']} plays.")
    fl = f.get("first_last") or {}
    if fl.get("first") and fl.get("last") and fl["first"]["title"] != fl["last"]["title"]:
        a, b = fl["first"], fl["last"]
        fact(f"First play of the period: {item(a['title'], a['media_type'])} {a['title']}; "
             f"last: {item(b['title'], b['media_type'])} {b['title']}.")
    c = f.get("completion")
    if c:
        fact(f"Finished {_pct(c['completion_rate'])}% of video plays; dropped {_pct(c['drop_rate'])}% early.")
    c = f.get("comfort")
    if c:
        fact(f"Comfort title {item(c['title'], c.get('media_type'))} {c['title']}: {c['replays']} replays; "
             f"{_pct(c['comfort_share'])}% of finished plays were rewatches.")
    b = f.get("binge") or {}
    if b.get("deepest_sitting"):
        s = b["deepest_sitting"]
        fact(f"Deepest sitting: {s['episodes']} episodes of {item(s['series'], s.get('media_type'))} "
             f"{s['series']} in one go on {s['date']}.")
    if b.get("in_48h"):
        s = b["in_48h"]
        fact(f"{s['episodes']} episodes of {item(s['series'], s.get('media_type'))} {s['series']} inside 48 hours.")
    e = f.get("eras")
    if e:
        o, n = e["oldest"], e["newest"]
        fact(f"Viewing age {e['viewing_age_year']} (the hour-weighted median release year). "
             f"Oldest {item(o['title'])} {o['title']} ({o['year']}), newest {item(n['title'])} "
             f"{n['title']} ({n['year']}).")
        sp = e["split"]
        fact(f"Release age: {_pct(sp.get('fresh'))}% this or last year, {_pct(sp.get('recent'))}% "
             f"2-7 years old, {_pct(sp.get('catalogue'))}% older.")
        if e.get("shortest") and e.get("longest"):
            s, lo = e["shortest"], e["longest"]
            fact(f"Shortest film {item(s['title'], 'movie')} {s['title']} ({s['minutes']} min), "
                 f"longest {item(lo['title'], 'movie')} {lo['title']} ({lo['minutes']} min).")
    h = f.get("highbrow_comfort")
    if h:
        line = (f"Time split: acclaimed {_pct(h['highbrow'])}%, comfort rewatches {_pct(h['comfort'])}%, "
                f"everything else {_pct(h['other'])}%.")
        if h.get("top_highbrow"):
            line += f" Top acclaimed pick {item(h['top_highbrow'])} {h['top_highbrow']}."
        fact(line)
    c = f.get("contrarian")
    if c:
        fact(f"Contrarian gem {item(c['title'], c.get('media_type'))} {c['title']}: you {c['you']}/10 "
             f"({c['basis']}), community {c['community']}/10 from {c['votes']} votes.")
    d = f.get("dismissal")
    if d:
        fact(f"Walked away from {item(d['title'], d.get('media_type'))} {d['title']} ({d['how']}), "
             f"community {d['community']}/10.")
    d = f.get("discovery")
    if d:
        fact(f"{_pct(d['new_share'])}% of the time went to titles new to you "
             f"({d['new_titles']} new, {d['returning_titles']} returning).")
    for cb in f.get("comebacks") or []:
        fact(f"Comeback: {item(cb['title'])} {cb['title']}, back after {cb['gap_days']} days away.")
    m = f.get("music")
    if m:
        for a in m.get("top_artists", [])[:3]:
            fact(f"Artist {item(a['artist'], 'music')} {a['artist']}: {a['hours']} hours, {a['plays']} plays.")
        if m.get("on_repeat"):
            r = m["on_repeat"]
            fact(f"On repeat: '{r['track']}' by {item(r['artist'], 'music')} {r['artist']}, "
                 f"{r['plays']} plays on {r['date']}.")
        if m.get("skip_rate") is not None:
            fact(f"Skipped {_pct(m['skip_rate'])}% of tracks on Spotify.")
        if m.get("most_skipped"):
            ms_ = m["most_skipped"]
            fact(f"Skipped most often: '{ms_['track']}' by {ms_['artist']}, {ms_['skips']} times.")
    g = f.get("genres")
    if g:
        fact("Top genres: " + ", ".join(f"{x['genre']} {_pct(x['share'])}%" for x in g["top"]) + ".")
        for x in g.get("rising", []):
            fact(f"Rising vs your past: {x['genre']} +{x['pp']} points.")
        for x in g.get("fading", []):
            fact(f"Fading vs your past: {x['genre']} {x['pp']} points.")
        if g.get("first_time"):
            fact(f"Genres played for the first time: {', '.join(g['first_time'])}.")
    cu = f.get("curatarr") or {}
    if cu.get("graveyard"):
        gy = cu["graveyard"]
        fact(f"Deleted {gy['deleted']} titles, {gy['gb_freed']} GB freed; biggest "
             f"{item(gy['biggest']['title'], gy['biggest'].get('media_type'))} {gy['biggest']['title']} "
             f"({gy['biggest']['gb']} GB).")
    if cu.get("came_back"):
        cb = cu["came_back"]
        fact(f"Deleted, then back in the library anyway: {item(cb['title'], cb.get('media_type'))} {cb['title']}.")
    if cu.get("shield"):
        s = cu["shield"]
        line = f"Kept {s['overrides']} titles over the curator's objection"
        if s.get("top_reason"):
            line += f", most often for '{s['top_reason']}'"
        line += "."
        if s.get("fiercest"):
            fr = s["fiercest"]
            line += f" Most recent: {item(fr['title'])} {fr['title']} — the curator called it \"{fr['curator_stance']}\"."
        fact(line)
    if cu.get("most_debated"):
        d = cu["most_debated"]
        fact(f"Most debated: {item(d['title'], d.get('media_type'))} {d['title']}, {d['messages']} "
             f"of your messages, ended {d['outcome']}.")
    for pr in cu.get("principles") or []:
        fact(f"New curation principle you established: \"{pr[:160]}\".")
    if cu.get("dust_collector"):
        d = cu["dust_collector"]
        uid = pick(d["title"], d.get("media_type"))
        fact(f"Dust collector {uid} {d['title']}: {d['gb']} GB on disk, never played by you.")
    for la in f.get("look_ahead") or []:
        uid = pick(la["title"], la.get("media_type"), la.get("poster"), la.get("year"),
                   la.get("reason"))
        line = f"Owned, unwatched pick {uid} {la['title']}"
        if la.get("year"):
            line += f" ({la['year']})"
        if la.get("reason"):
            line += f": {la['reason']}"
        fact(line + ".")
    dr = f.get("drift") or {}
    if dr:
        fact(f"Taste drift against your own past: {dr['label']}.")
        for cl in dr.get("clusters") or []:
            clusters[cl["id"]] = cl
            fact(f"Cluster {cl['id']} around {', '.join(cl['top_titles'])}: {_pct(cl['share'])}% now, "
                 f"{_pct(cl['share_before'])}% before ({cl['trend']}).")

    c = f.get("court") or {}
    if c:
        fact(f"Debates decided: {c['decided']} ({c['kept']} kept, {c['deleted']} deleted, "
             f"{c['overrides']} kept over the curator's objection); you won {c['won']}, "
             f"the curator won {c['lost']}.")
        if c.get("boldest_keep"):
            b = c["boldest_keep"]
            fact(f"Boldest keep: {item(b['title'], b.get('media_type'))} {b['title']}, which the judge "
                 f"was {b['score']}% sure should go ({c['defiant']} kept at 80% or more).")
        if c.get("talked_into"):
            t = c["talked_into"]
            fact(f"The curator talked you into deleting {item(t['title'], t.get('media_type'))} "
                 f"{t['title']} after {t['turns']} of your messages.")
        if c.get("talked_out_of"):
            t = c["talked_out_of"]
            fact(f"You talked the curator out of deleting {item(t['title'], t.get('media_type'))} {t['title']}.")
    v = f.get("velocity") or {}
    if v:
        fact(f"Server: {v['gb_in']} GB arrived, {v['gb_out']} GB left ({v['verdict']}).")
    t = f.get("time_to_watch") or {}
    if t:
        lo = t["longest"]
        fact(f"New titles waited a median {t['median_days']} days before the first play; longest "
             f"{item(lo['title'], lo.get('media_type'))} {lo['title']}, {lo['days']} days; "
             f"{_pct(t['same_week_share'])}% played within a week of arriving.")
    for role, x in (f.get("auteurs") or {}).items():
        refs = ", ".join(f"{item(n)} {n}" for n in x["titles"][:3])
        fact(f"Most-watched {role}: {x['name']}, {x['count']} titles, {x['hours']} hours ({refs}).")
    from src.services.wrapup_insights import mine, sheet_block
    worth = sheet_block(mine(f))

    traits = (f.get("traits") or [])[:3]
    sheet = "\n".join(facts)
    if worth:
        sheet += "\n\n" + worth
    if traits:
        sheet += "\n\nTRAITS (pick one as archetype_trait): " + "; ".join(
            f"{tr['key']} ({tr['evidence']})" for tr in traits)
    allowed = numbers_in(sheet)
    return sheet, {"facts": {x.split(":", 1)[0] for x in facts}, "items": items,
                   "picks": picks, "clusters": clusters, "numbers": allowed,
                   "traits": [tr["key"] for tr in traits]}


def finish_story(story: dict, idx: dict) -> dict:
    """What the UI needs next to the story: every ref resolved to its title and
    artwork, so the page never looks a title up by the model's spelling."""
    return {**story, "refs": {**idx["items"], **idx["picks"]},
            "clusters": {k: {"top_titles": v.get("top_titles")} for k, v in idx["clusters"].items()}}


# ── the deterministic stand-in ──────────────────────────────────────────────

TRAIT_TITLES = {
    "night_owl": ("The Midnight Shift", "Most of it happened after the house went quiet."),
    "matinee": ("The Weekend Matinee", "Weekend afternoons did the heavy lifting."),
    "completionist": ("The Completionist", "Started it, finished it."),
    "sampler": ("The Sampler", "Plenty of first episodes, fewer second ones."),
    "scout": ("The Scout", "Mostly things you had never played before."),
    "loyalist": ("The Loyalist", "Old friends got most of the hours."),
    "archivist": ("The Archivist", "Your viewing age is older than your hardware."),
    "day_one": ("Day One", "If it came out this year, you were there."),
    "binger": ("The Binger", "One more episode was never one."),
    "highbrow": ("The Highbrow", "The acclaimed shelf did not gather dust."),
    "comfort_seeker": ("The Comfort Rewatcher", "Familiar ground, by choice."),
    "contrarian": ("The Contrarian", "Your favourites are not the crowd's."),
    "purger": ("The Purger", "The server is lighter for it."),
    "defender": ("The Defender", "Some titles stay because you said so."),
}


def fallback_story(f: dict, idx: dict) -> dict:
    """The same shape, written from the facts alone: archetype from the
    strongest trait, chapters from the top titles, every sentence a fact."""
    traits = f.get("traits") or []
    key = traits[0]["key"] if traits else None
    title, sub = TRAIT_TITLES.get(key, ("Your Period in Review", "The numbers, without the commentary."))
    by_title = {v["title"]: k for k, v in idx["items"].items()}
    top = f.get("top") or []
    chapters = []
    if top:
        refs = [by_title[t["title"]] for t in top[:2] if t["title"] in by_title]
        lead = top[0]
        chapters.append({"title": "Where the hours went", "kicker": f"{lead['title']} led the period",
                         "commentary": f"{lead['title']} took {lead['hours']} hours over {lead['plays']} plays.",
                         "item_refs": refs, "fact_refs": []})
    rest = [by_title[t["title"]] for t in top[2:5] if t["title"] in by_title]
    if rest:
        chapters.append({"title": "The rest of the shelf", "kicker": "Also in heavy rotation",
                         "commentary": ", ".join(idx["items"][r]["title"] for r in rest) + ".",
                         "item_refs": rest, "fact_refs": []})
    pick = next(iter(idx["picks"]), None)
    hook = None
    if pick:
        hook = {"challenge": f"{idx['picks'][pick]['title']} is on the server and you have not played it yet.",
                "item_ref": pick, "fact_refs": []}
    return {"archetype_title": title, "archetype_subhead": sub, "archetype_trait": key,
            "era_chapters": chapters, "roast_and_toast": None, "look_ahead_hook": hook,
            "cluster_names": [], "share_line": title}


# ── the background task ─────────────────────────────────────────────────────

def _voice(user_id: int, cards: list) -> tuple:
    """(language, quotes): the language the user writes to the curator in,
    and — unless they turned it off — their own words from the debates about
    the cards' titles. Blocking."""
    from src.database.connection import get_db_session
    from src.services.wrapup_writer import main_language, quotes_allowed, user_quotes
    with get_db_session() as db:
        lang = main_language(db, user_id)
        titles = list(dict.fromkeys(r for c in cards for r in c.get("refs") or []))
        quotes = user_quotes(db, user_id, titles) if quotes_allowed(user_id) else []
    return lang, quotes


async def generate_wrapup(user_id: int, scope: str, period_key: str, is_admin: bool, task) -> None:
    """Facts → drift → artwork → story → checks → stored. Reports each phase
    to the task monitor (the page's ticker reads it)."""
    from src.database.runner import run_read, run_write
    from src.routers.wrapup import attach_posters
    from src.services.task_monitor import task_monitor
    from src.services.wrapup_facts import build_wrapup_facts, taste_drift, taste_river
    from src.services.wrapup_insights import mine
    from src.services.wrapup_store import save_error, save_facts, save_story
    from src.services.wrapup_writer import write

    def step(i):
        task_monitor.update(task, processed=i, message=PHASES[i])

    try:
        step(0)
        facts = await run_read(build_wrapup_facts, user_id, scope, period_key, is_admin=is_admin)
        step(1)
        if facts.get("empty") or not facts.get("eligible"):
            task_monitor.skip(task, "not enough played in this period yet")
            return
        step(2)
        drift = await taste_drift(user_id, scope, period_key)
        if drift:
            facts["drift"] = drift
        river = await taste_river(user_id, scope, period_key)
        if river:
            facts["river"] = river
        step(3)
        await attach_posters(facts)
        await run_write(save_facts, user_id, scope, period_key, facts)
        step(4)
        sheet, idx = render_fact_sheet(facts)
        cards = mine(facts)
        lang, quotes = await run_read(_voice, user_id, cards)
        story, source, model = await write(facts, sheet, idx, cards, quotes=quotes, lang=lang)
        step(5)
        if story is None:
            logger.info("[wrapup] %s/%s/%s: deterministic story (%s)", user_id, scope, period_key, source)
            story, source = fallback_story(facts, idx), "fallback"
        await run_write(save_story, user_id, scope, period_key, facts["facts_hash"],
                        finish_story(story, idx), source, model)
        task_monitor.done(task, "Wrap-up written" if source != "fallback" else "Wrap-up written without the curator")
    except Exception as e:                  # a task must never die silently
        logger.warning("[wrapup] generation failed for %s/%s/%s: %s", user_id, scope, period_key, e)
        try:
            await run_write(save_error, user_id, scope, period_key, str(e))
        except Exception as e2:
            logger.warning("[wrapup] could not record the error: %s", e2)
        task_monitor.error(task, str(e)[:200])


# ── "Ask your wrap-up": the chat thread's hidden context ───────────────────

def wrapup_discussion_block(facts: dict, story: Optional[dict]) -> str:
    """The discuss-context block for a chat thread about one wrap-up: its fact
    sheet and what the page said. The chat answers from these numbers."""
    sheet, _ = render_fact_sheet(facts)
    block = ("[CURRENT DISCUSSION CONTEXT]\n"
             "The user is looking at their wrap-up and wants to talk about it.\n"
             "WRAP-UP FACTS (computed from their own history — the only numbers you may quote):\n"
             f"{sheet}\n")
    if story:
        block += f"WHAT THE PAGE SAYS: archetype \"{story.get('archetype_title', '')}\""
        if story.get("archetype_subhead"):
            block += f" — {story['archetype_subhead']}"
        block += "\n"
        for c in story.get("era_chapters") or []:
            block += f"  Chapter \"{c.get('title', '')}\": {c.get('commentary', '')}\n"
        rt = story.get("roast_and_toast") or {}
        if rt:
            block += f"  Roast: {rt.get('roast', '')}\n  Toast: {rt.get('toast', '')}\n"
    block += ("Answer their questions from these facts. If they ask for a number that is not "
              "here, say the wrap-up does not have it rather than estimating one. You may "
              "disagree with what the page said — it was your own first draft.\n")
    return block


# ── the season: written ahead, so the page opens with a story ──────────────

_REFRESH_DAYS = 7


def due_wrapups(row_for, now=None) -> list:
    """(scope, period) pairs worth writing ahead for one user, given
    ``row_for(scope, period)`` → the stored row or None. The rolling month is
    kept at most a week old; from 1 December the current year is written
    (and refreshed weekly while it fills), and in the first two weeks of
    January the closed year once more, so the final version covers every day."""
    from datetime import datetime, timedelta
    now = now or datetime.utcnow()
    out = []

    def stale(row, written_after=None):
        if not row or not row.get("story"):
            return True
        try:
            at = datetime.fromisoformat(row["story_at"])
        except (TypeError, ValueError):
            return True
        if written_after is not None:
            return at < written_after
        return now - at > timedelta(days=_REFRESH_DAYS) or row.get("story_hash") != row.get("facts_hash")

    if stale(row_for("all", "30d")):
        out.append(("all", "30d"))
    if now.month == 12 and stale(row_for("all", f"year:{now.year}")):
        out.append(("all", f"year:{now.year}"))
    if now.month == 1 and now.day <= 14:
        closed = f"year:{now.year - 1}"
        if stale(row_for("all", closed), written_after=datetime(now.year, 1, 1)):
            out.append(("all", closed))
    return out


async def pregenerate_all(task=None) -> bool:
    """Custodian task: write the due wrap-ups of every active user in the
    background (one at a time — each is a curator call). A user who is
    writing one by hand right now is skipped for that wrap-up."""
    from src.database.connection import get_db_session
    from src.database.models import User
    from src.database.runner import run_read
    from src.services.task_monitor import task_monitor
    from src.services.wrapup_store import load

    def _users():
        with get_db_session() as db:
            return [(u.id, bool(u.is_admin), u.plex_username)
                    for u in db.query(User).filter(User.is_active == True).all()]  # noqa: E712

    def _due(uid):
        return due_wrapups(lambda scope, period: load(uid, scope, period))

    ok = True
    users = await run_read(_users)
    for i, (uid, admin, name) in enumerate(users):
        for scope, period in await run_read(_due, uid):
            tid = f"wrapup-{uid}-{scope}-{period}"
            if any(t["id"] == tid for t in task_monitor.get_running()):
                continue
            if task is not None:
                task_monitor.update(task, processed=i, total=len(users),
                                    message=f"Writing {name}'s wrap-up ({period})…")
            sub = task_monitor.create(f"Wrap-up: {scope} · {period}", "wrapup",
                                      total=len(PHASES), task_id=tid)
            task_monitor.start(sub)
            await generate_wrapup(uid, scope, period, admin, sub)
            if sub.status.value == "error":
                ok = False
    return ok
