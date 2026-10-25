"""Curatarr — the insight miner: which facts are worth saying, and why.

The v1 fact sheet handed the curator ~40 equally weighted lines: nothing told
it that a 9-vs-6.4 contrarian gap is remarkable and "peak hour 21:00" is not,
and every tile rendered because data existed, not because it was
interesting. The miner turns the facts (services/wrapup_facts) into ranked
claim cards:

    {"id": "I03", "kind": "contrast", "slide": "twist", "score": 0.82,
     "claim": "You rated The Bear 9; the community gives it 6.4 (2,100 votes).",
     "why": "the widest gap between you and everyone else this period",
     "values": {...}, "say": {"gap": ["2.6 points", "two and a half points"]},
     "refs": ["The Bear"]}

* ``score`` (0..1) is how much the fact surprises: distance from what is
  usual, specificity (it names a title, a day, a person), and contrast (two
  facts pulling apart). Below ``FLOOR`` a card is dropped — a quiet period
  gets a shorter story, not padding.
* ``say`` holds the approved renderings of each number, rounding included
  ("58.3 hours" → "58 hours", "nearly 60 hours", "two and a half days"):
  the writer may round, from this list. The fact sheet carries them, so the
  number check accepts them.
* ``why`` says in plain words what makes it interesting — exactly what the
  flat sheet withheld.
* ``slide`` is the story beat the card belongs to (services/wrapup_story's
  arc): open, reveal, rhythm, moment, twist, court, balance, identity, music.

Pure: facts in, cards out; nothing is read or written.
"""
from typing import Optional

FLOOR = 0.35
MAX_CARDS = 12

_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
          8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve"}
_FRACTIONS = ((0.25, "a quarter"), (1 / 3, "a third"), (0.5, "half"),
              (2 / 3, "two thirds"), (0.75, "three quarters"))


# ── renderings ───────────────────────────────────────────────────────────────

def _fmt(x) -> str:
    """58.3 → '58.3', 20.0 → '20', 2100 → '2,100'."""
    x = float(x or 0)
    return f"{x:,.1f}".rstrip("0").rstrip(".") if round(x % 1, 1) else f"{int(round(x)):,}"


def say_count(n, noun: str = "") -> list:
    n = int(n or 0)
    tail = f" {noun}" if noun else ""
    out = [f"{n:,}{tail}"]
    if n in _WORDS:
        out.append(f"{_WORDS[n]}{tail}")
    return out


def say_hours(h) -> list:
    """58.3 → 58.3 hours, 58 hours, nearly 60 hours, two and a half days."""
    h = float(h or 0)
    out = [f"{_fmt(round(h, 1))} hours"]
    if h >= 2:
        out.append(f"{round(h):,} hours")
    if h >= 10:
        up, down = (int(h // 10) + 1) * 10, int(h // 10) * 10
        if up - h <= 3:
            out.append(f"nearly {up:,} hours")
        elif h - down <= 3 and down:
            out.append(f"just over {down:,} hours")
    if h >= 48:
        days = h / 24
        half = round(days * 2) / 2
        whole = int(half)
        words = _WORDS.get(whole, f"{whole:,}")
        out.append(f"{words} and a half days" if half % 1 else f"{words} full days")
    elif 1.5 <= h < 2.25:
        out.append("about two hours")
    return list(dict.fromkeys(out))


def say_minutes(m) -> list:
    m = int(m or 0)
    out = [f"{m} minutes"]
    if m >= 90:
        h = m / 60
        out.append(f"nearly {int(h) + 1} hours" if (int(h) + 1) * 60 - m <= 10
                   else f"{_fmt(round(h, 1))} hours")
    return out


def say_share(x) -> list:
    """0.42 → 42%, two in five; 0.5 → half."""
    x = float(x or 0)
    out = [f"{round(x * 100)}%"]
    for v, words in _FRACTIONS:
        if abs(x - v) <= 0.03:
            out.append(words)
            break
    else:
        for d in (5, 10):
            n = round(x * d)
            if 0 < n < d and abs(x - n / d) <= 0.02:
                out.append(f"{_WORDS.get(n, n)} in {_WORDS.get(d, d)}")
                break
    return out


def say_days(d) -> list:
    d = int(d or 0)
    out = say_count(d, "days" if d != 1 else "day")
    if d >= 330:
        years = d / 365
        out.append("over a year" if years < 1.5 else f"over {_WORDS.get(int(years), int(years))} years")
    elif d >= 50:
        out.append(f"{_WORDS.get(round(d / 30), round(d / 30))} months")
    elif d >= 14:
        out.append(f"{_WORDS.get(round(d / 7), round(d / 7))} weeks")
    return list(dict.fromkeys(out))


def say_gb(gb) -> list:
    gb = float(gb or 0)
    out = [f"{_fmt(round(gb, 1))} GB"]
    if gb >= 1000:
        out.append(f"{_fmt(round(gb / 1024, 1))} TB")
    elif gb >= 10:
        out.append(f"{round(gb):,} GB")
    return list(dict.fromkeys(out))


def say_score(x) -> list:
    return [f"{_fmt(round(float(x), 1))}/10", _fmt(round(float(x), 1))]


# ── the miners ───────────────────────────────────────────────────────────────

def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _card(kind, slide, score, claim, why, values=None, say=None, refs=None,
          media_type: Optional[str] = None) -> dict:
    refs = [r for r in (refs or []) if r]
    # specificity: a card that names something beats one that does not
    score = _clamp(score + (0.08 if refs else 0))
    return {"kind": kind, "slide": slide, "score": round(score, 2), "claim": claim,
            "why": why, "values": values or {}, "say": say or {}, "refs": refs,
            "media_type": media_type}


def _tempo(f) -> list:
    t, out = f.get("tempo") or {}, []
    if not t:
        return out
    h = t.get("hours") or 0
    prev = t.get("prev_hours")
    if prev:
        delta = (h - prev) / prev
        if abs(delta) >= 0.25:
            up = delta > 0
            out.append(_card(
                "contrast", "open", 0.35 + _clamp(abs(delta) / 2) * 0.5,
                f"{_fmt(h)} hours, against {_fmt(prev)} in the same span before.",
                f"{'more' if up else 'less'} than the span before by {round(abs(delta) * 100)}%",
                {"hours": h, "prev_hours": prev, "delta": round(delta, 2)},
                {"hours": say_hours(h), "prev_hours": say_hours(prev),
                 "delta": say_share(abs(delta))}))
    night = (t.get("dayparts") or {}).get("night", 0)
    if night >= 0.25:
        out.append(_card("rhythm", "rhythm", 0.3 + night,
                         f"{round(night * 100)}% of it after 23:00.",
                         "most people's viewing ends before the night begins",
                         {"night": night}, {"night": say_share(night)}))
    streak = t.get("longest_streak_days") or 0
    if streak >= 5:
        out.append(_card("streak", "rhythm", 0.25 + _clamp(streak / 21) * 0.65,
                         f"A {streak}-day run without a day off.",
                         "an unbroken run of days is effort made visible",
                         {"days": streak}, {"days": say_days(streak)}))
    bd = t.get("biggest_day") or {}
    active = t.get("active_days") or 0
    if bd and active >= 5 and h:
        ratio = (bd.get("hours") or 0) / (h / active)
        if ratio >= 2.5:
            out.append(_card("moment", "moment", 0.3 + _clamp((ratio - 2.5) / 5) * 0.5,
                             f"{bd['date']}: {_fmt(bd['hours'])} hours in one day, {bd['plays']} plays.",
                             f"about {round(ratio)} times a usual day",
                             {"date": bd["date"], "hours": bd["hours"], "ratio": round(ratio, 1)},
                             {"hours": say_hours(bd["hours"]), "ratio": say_count(round(ratio), "times")}))
    return out


def _top(f) -> list:
    t, top = f.get("tempo") or {}, f.get("top") or []
    if not top or not t.get("hours"):
        return []
    first = top[0]
    share = (first.get("hours") or 0) / t["hours"]
    lead = share / max(0.01, (top[1]["hours"] / t["hours"]) if len(top) > 1 else 0.01)
    return [_card("superlative", "reveal", 0.35 + _clamp(share * 1.5) * 0.4 + _clamp((lead - 1) / 4) * 0.2,
                  f"{first['title']} took {_fmt(first['hours'])} hours, {round(share * 100)}% of the time.",
                  "the title that owned the period" + (f", well clear of {top[1]['title']}"
                                                       if len(top) > 1 and lead >= 2 else ""),
                  {"hours": first["hours"], "share": round(share, 2), "plays": first.get("plays")},
                  {"hours": say_hours(first["hours"]), "share": say_share(share)},
                  [first["title"]], first.get("media_type"))]


def _binge(f) -> list:
    b, out = f.get("binge") or {}, []
    s = b.get("deepest_sitting")
    if s:
        out.append(_card("streak", "moment", 0.25 + _clamp(s["episodes"] / 12) * 0.6,
                         f"{s['episodes']} episodes of {s['series']} in one sitting on {s['date']}.",
                         "one more episode, again and again", {"episodes": s["episodes"]},
                         {"episodes": say_count(s["episodes"], "episodes")},
                         [s["series"]], s.get("media_type")))
    return out


def _contrasts(f) -> list:
    out = []
    c = f.get("contrarian")
    if c:
        gap = c.get("gap") or 0
        rated = c.get("basis") == "rated"
        out.append(_card("contrast", "twist", (0.3 + _clamp(gap / 3.5) * 0.6) * (1 if rated else 0.7),
                         f"You {'rated' if rated else 'kept rewatching'} {c['title']}"
                         f"{' ' + _fmt(c['you']) if rated else ''}; the community gives it "
                         f"{c['community']} ({c['votes']:,} votes).",
                         "the widest gap between you and everyone else",
                         {"you": c["you"], "community": c["community"], "gap": gap},
                         {"community": say_score(c["community"]),
                          "gap": [f"{_fmt(gap)} points"] + (["two and a half points"] if 2.4 <= gap <= 2.6 else []),
                          "votes": say_count(c["votes"], "votes")},
                         [c["title"]], c.get("media_type")))
    d = f.get("dismissal")
    if d:
        out.append(_card("contrast", "twist", 0.45 + _clamp((d["community"] - 7.8) / 1.2) * 0.4,
                         f"You walked away from {d['title']} ({d['how']}); the community gives it "
                         f"{d['community']}.",
                         "acclaim did not keep you there", {"community": d["community"]},
                         {"community": say_score(d["community"])}, [d["title"]], d.get("media_type")))
    cf = f.get("comfort")
    if cf and (cf.get("replays") or 0) >= 3:
        out.append(_card("confession", "moment", 0.25 + _clamp(cf["replays"] / 8) * 0.5,
                         f"{cf['title']} again: {cf['replays']} replays.",
                         "a comfort title says more than a top list", {"replays": cf["replays"]},
                         {"replays": say_count(cf["replays"], "replays")},
                         [cf["title"]], cf.get("media_type")))
    return out


def _court(f) -> list:
    c, out = f.get("court") or {}, []
    if not c:
        return out
    bk = c.get("boldest_keep")
    if bk:
        out.append(_card("court", "court", 0.55 + _clamp((bk["score"] - 80) / 20) * 0.35,
                         f"The judge was {bk['score']}% sure {bk['title']} should go. It stayed.",
                         "you overruled the curator where it was surest",
                         {"score": bk["score"], "defiant": c.get("defiant")},
                         {"score": [f"{bk['score']}%"], "defiant": say_count(c.get("defiant"), "titles")},
                         [bk["title"]], bk.get("media_type")))
    if (c.get("won") or 0) + (c.get("lost") or 0) >= 2:
        won, lost = c.get("won", 0), c.get("lost", 0)
        out.append(_card("court", "court", 0.45 + _clamp(abs(won - lost) / 5) * 0.35,
                         f"Debates: you talked the curator out of {won}, it talked you into {lost}.",
                         "who usually wins the argument", {"won": won, "lost": lost},
                         {"won": say_count(won), "lost": say_count(lost)},
                         [x["title"] for x in (c.get("talked_out_of"), c.get("talked_into")) if x]))
    elif c.get("decided", 0) >= 3:
        out.append(_card("court", "court", 0.4,
                         f"{c['decided']} titles decided: {c['kept']} kept, {c['deleted']} deleted, "
                         f"{c['overrides']} over the curator's objection.",
                         "the year's verdicts", {k: c[k] for k in ("decided", "kept", "deleted", "overrides")},
                         {k: say_count(c[k]) for k in ("decided", "kept", "deleted", "overrides")}))
    return out


def _balance(f) -> list:
    out = []
    v = f.get("velocity") or {}
    if v and (v.get("gb_in") or v.get("gb_out")):
        net = v.get("net_gb") or 0
        big = max(v.get("gb_in") or 0, v.get("gb_out") or 0) or 1
        verdict = v.get("verdict")
        out.append(_card("balance", "balance", 0.35 + _clamp(abs(net) / big) * 0.45,
                         f"{_fmt(v['gb_in'])} GB in, {_fmt(v['gb_out'])} GB out — "
                         f"{'a pruner' if verdict == 'pruner' else 'a hoarder' if verdict == 'hoarder' else 'in balance'}.",
                         "what the server gained against what it lost",
                         {"gb_in": v["gb_in"], "gb_out": v["gb_out"], "net_gb": net},
                         {"gb_in": say_gb(v["gb_in"]), "gb_out": say_gb(v["gb_out"]),
                          "net_gb": say_gb(abs(net))},
                         [x["title"] for x in (v.get("biggest_in"), v.get("biggest_out")) if x]))
    t = f.get("time_to_watch") or {}
    if t:
        lo = t["longest"]
        out.append(_card("confession", "balance", 0.3 + _clamp(lo["days"] / 400) * 0.6,
                         f"{lo['title']} waited {lo['days']} days on the server before you pressed play.",
                         "the longest anything sat in the queue",
                         {"days": lo["days"], "median_days": t["median_days"]},
                         {"days": say_days(lo["days"]), "median_days": say_days(t["median_days"])},
                         [lo["title"]], lo.get("media_type")))
        if t.get("same_week_share", 0) >= 0.6:
            out.append(_card("rhythm", "balance", 0.3 + (t["same_week_share"] - 0.6),
                             f"{round(t['same_week_share'] * 100)}% of the new titles played within a week "
                             "of arriving.", "no backlog: things are watched as they land",
                             {"share": t["same_week_share"]}, {"share": say_share(t["same_week_share"])}))
    return out


def _identity(f) -> list:
    out = []
    a = f.get("auteurs") or {}
    for role, label in (("director", "director"), ("studio", "studio"), ("actor", "face")):
        x = a.get(role)
        if x:
            out.append(_card("identity", "identity", 0.3 + _clamp((x["count"] - 1) / 4) * 0.4
                             + _clamp(x["hours"] / 40) * 0.2,
                             f"Your {label}: {x['name']}, {x['count']} titles, {_fmt(x['hours'])} hours "
                             f"({', '.join(x['titles'][:3])}).",
                             f"the {label} behind most of the hours",
                             {"count": x["count"], "hours": x["hours"]},
                             {"count": say_count(x["count"], "titles"), "hours": say_hours(x["hours"])},
                             x["titles"][:3]))
    d = f.get("drift") or {}
    if d.get("label") in ("shifting", "new direction"):
        out.append(_card("identity", "identity", 0.55 if d["label"] == "shifting" else 0.8,
                         f"Taste against your own past: {d['label']}.",
                         "the period did not look like the ones before it", {"drift": d.get("drift")}))
    river = [m for m in (f.get("river") or []) if m.get("similarity") is not None]
    if river:
        turn = min(river, key=lambda m: m["similarity"])
        if turn["similarity"] < 0.5:
            out.append(_card("moment", "identity", 0.45 + _clamp((0.5 - turn["similarity"]) / 0.5) * 0.4,
                             f"{turn['month']} was the month it turned: "
                             f"{', '.join(turn['top_titles'][:2])}.",
                             "the month least like the one before it", {"month": turn["month"]},
                             refs=turn["top_titles"][:2]))
    ds = f.get("discovery") or {}
    if ds and (ds.get("new_share", 0.5) >= 0.7 or ds.get("new_share", 0.5) <= 0.2):
        new = ds["new_share"]
        out.append(_card("identity", "identity", 0.3 + abs(new - 0.5) * 0.6,
                         f"{round(new * 100)}% of the time on titles new to you.",
                         "a scout" if new >= 0.7 else "a loyalist: old friends got the hours",
                         {"new_share": new}, {"new_share": say_share(new)},
                         [ds.get("best_new")] if new >= 0.7 else None))
    return out


def _music(f) -> list:
    m, out = f.get("music") or {}, []
    r = m.get("on_repeat")
    if r:
        out.append(_card("moment", "music", 0.3 + _clamp(r["plays"] / 15) * 0.55,
                         f"'{r['track']}' by {r['artist']}, {r['plays']} times on {r['date']}.",
                         "one track, on repeat, all day", {"plays": r["plays"]},
                         {"plays": say_count(r["plays"], "times")}, [r["artist"]], "music"))
    sk = m.get("most_skipped")
    if sk:
        out.append(_card("confession", "music", 0.35 + _clamp(sk["skips"] / 15) * 0.4,
                         f"'{sk['track']}' by {sk['artist']}: skipped {sk['skips']} times.",
                         "the track you keep not wanting", {"skips": sk["skips"]},
                         {"skips": say_count(sk["skips"], "times")}, [sk["artist"]], "music"))
    return out


def _moods(f) -> list:
    mo = (f.get("music") or {}).get("moods") or {}
    parts = mo.get("by_daypart") or {}
    out = []
    day = [(p, parts[p]) for p in ("morning", "afternoon") if p in parts]
    late = [(p, parts[p]) for p in ("evening", "night") if p in parts]
    if day and late:
        (dp, dx), (lp, lx) = max(day, key=lambda x: x[1]["hours"]), max(late, key=lambda x: x[1]["hours"])
        if dx["mood"] != lx["mood"]:
            out.append(_card("contrast", "music", 0.4 + _clamp((dx["share"] + lx["share"]) / 2) * 0.4,
                             f"{dp.capitalize()}s ran {dx['mood']}, {lp}s {lx['mood']}.",
                             "the same person, two soundtracks a day apart",
                             {"day_share": dx["share"], "late_share": lx["share"]},
                             {"day_share": say_share(dx["share"]), "late_share": say_share(lx["share"])},
                             media_type="music"))
    top = (mo.get("top_moods") or [None])[0]
    if top and top["share"] >= 0.3:
        out.append(_card("identity", "music", 0.25 + _clamp(top["share"]) * 0.45,
                         f"{round(top['share'] * 100)}% of the tagged listening was {top['mood']}.",
                         "the mood the year kept coming back to", {"share": top["share"]},
                         {"share": say_share(top["share"])}, media_type="music"))
    return out


def _devices(f) -> list:
    n = (f.get("devices") or {}).get("night")
    if not n or n["share"] < 0.5:
        return []
    return [_card("rhythm", "rhythm", 0.3 + _clamp(n["share"]) * 0.4 + _clamp(n["hours"] / 50) * 0.15,
                  f"After 23:00 it was the {n['player']}: {round(n['share'] * 100)}% of {_fmt(n['hours'])} night hours.",
                  "where the late hours were spent", {"share": n["share"], "hours": n["hours"]},
                  {"share": say_share(n["share"]), "hours": say_hours(n["hours"])})]


_MINERS = (_tempo, _top, _binge, _contrasts, _court, _balance, _identity, _music, _moods, _devices)


def mine(facts: dict, floor: float = FLOOR, limit: int = MAX_CARDS) -> list:
    """The cards worth saying, highest score first, numbered I01…"""
    if not facts or facts.get("empty"):
        return []
    cards = [c for miner in _MINERS for c in miner(facts)]
    cards = sorted((c for c in cards if c["score"] >= floor), key=lambda c: -c["score"])[:limit]
    for i, c in enumerate(cards, 1):
        c["id"] = f"I{i:02d}"
    return cards


def sheet_block(cards: list) -> str:
    """The ranked block the fact sheet ends with: what is worth saying, why,
    and the renderings each number may take."""
    if not cards:
        return ""
    lines = ["WORTH SAYING (ranked — build the story from the top; the rest is background):"]
    for c in cards:
        say = "; ".join(f"{k}: " + " | ".join(v) for k, v in c["say"].items() if v)
        lines.append(f"{c['id']} [{c['slide']}, {c['score']}] {c['claim']} — why: {c['why']}."
                     + (f" Say: {say}." if say else ""))
    return "\n".join(lines)
