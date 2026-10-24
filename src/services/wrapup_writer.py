"""Curatarr — the wrap-up writer: an editor picks the story, a writer tells it.

v1 asked the curator, in one JSON-forced answer, to find the story, write it
and fill a 15-field form, then checked the result against rules that were
strict on style and loose on truth. A rounded number, a title with an "!" or
a band called Journey sent the whole story back; one repair turn named only
the first fault; the second failure threw away every good field for a
deterministic stand-in. Meanwhile a wrong number that happened to appear
elsewhere on the sheet passed, and so did a title nobody had cited.

v2 splits the work (docs/WRAPUP_V2_BLUEPRINT.md §4.1):

1. **Editor** (JSON — a choice, which constrained formats are good at): picks
   3-7 of the insight cards (services/wrapup_insights) in story order, the
   roast and the toast card, the archetype from the earned traits, a share
   line, the look-ahead pick and names for the taste clusters.
2. **Writer** (free prose): one ``## I03 | headline`` section per chosen
   card plus ROAST, TOAST and NEXT, each sentence ending with the cards it
   rests on in brackets — ``[I03]`` or ``[I03, U1]``.
3. **Checks, strict on truth and lenient on style.** Per sentence: a
   citation is required; its numerals must be the cited cards' own (values,
   claim, approved renderings) or 0-12; a library title it names must be one
   the cited cards reference. Style is fixed, never fatal: titles and artist
   names are masked first (so *Mamma Mia!* and Journey are safe), then stock
   phrases are cut and exclamation marks become full stops.
4. **Repair per slide.** The slides that failed go back once, every fault
   listed; one that fails again gets its card's deterministic template. The
   other slides stay as written.

The curator may quote the user's own debate messages back to them
("callbacks") — only in their own wrap-up, and only while they have not
turned it off (``quotes_allowed``). It writes in the language the user writes
to it most (``main_language``).
"""
import logging
import re
from collections import Counter
from typing import Optional

logger = logging.getLogger(__name__)

QUOTES_OFF_KEY = "wrapup_quotes_off:{uid}"
_QUOTE_MIN, _QUOTE_MAX, _QUOTES = 20, 200, 4
_TEXT_MAX, _HEAD_MAX = 700, 70
_ARC_MIN, _ARC_MAX = 3, 7

_TEMP_EDITOR, _TEMP_WRITER = 0.4, 0.85
_PREDICT_EDITOR, _PREDICT_WRITER, _PREDICT_REPAIR = 500, 1800, 1200
_TIMEOUT = 420.0

BEAT_HEADLINES = {"open": "The period in hours", "reveal": "Where the hours went",
                  "rhythm": "When you watched", "moment": "The day it happened",
                  "twist": "The twist", "court": "Before the court", "balance": "In and out",
                  "identity": "Who you were", "music": "On repeat"}

# ── language ────────────────────────────────────────────────────────────────

_STOPWORDS = {
    "en": "the and is it that you to of this was not but have what with for".split(),
    "de": "der die das und ist nicht ich ein eine es mit auf zu den sich auch".split(),
    "fr": "le la les et est pas je une un que des du pour avec ce".split(),
    "es": "el la los las y es no que un una de por con para lo".split(),
    "it": "il la e è non che un una di per con lo gli sono".split(),
    "nl": "de het een en is niet ik dat van met op voor maar ook".split(),
}
LANG_NAMES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish",
              "it": "Italian", "nl": "Dutch"}


def detect_language(texts: list) -> str:
    """The language most of these messages are written in, by stopwords —
    no model. English when nothing tips it."""
    score = Counter()
    for t in texts:
        words = re.findall(r"[a-zà-ÿäöüß]+", (t or "").lower())
        if not words:
            continue
        per = {lang: sum(1 for w in words if w in set(sw)) for lang, sw in _STOPWORDS.items()}
        best = max(per, key=per.get)
        if per[best]:
            score[best] += 1
    return score.most_common(1)[0][0] if score else "en"


def main_language(db, user_id: int) -> str:
    """The language the user writes to the curator in most (their last 300
    messages). Blocking."""
    from src.database.models import ConversationMessage as M
    rows = (db.query(M.content).filter(M.user_id == user_id, M.role == "user")
            .order_by(M.id.desc()).limit(300).all())
    return detect_language([c for (c,) in rows])


# ── the user's own words ────────────────────────────────────────────────────

def quotes_allowed(user_id: int) -> bool:
    """Opt-out: quotes only ever reach the person who wrote them. Blocking."""
    from src.services.app_state import get_state
    return get_state(QUOTES_OFF_KEY.format(uid=user_id)) != "1"


def set_quotes_allowed(user_id: int, allowed: bool) -> None:
    from src.services.app_state import set_state
    set_state(QUOTES_OFF_KEY.format(uid=user_id), "" if allowed else "1")


def user_quotes(db, user_id: int, titles: list) -> list:
    """[{title, text}] — the user's own words from the deletion debates about
    these titles: one per title, the newest that reads as a statement."""
    from src.database.models import ConversationMessage as M, DeletionProposal as P
    out = []
    for title in titles:
        ids = [pid for (pid,) in db.query(P.id).filter(P.user_id == user_id, P.title == title)]
        if not ids:
            continue
        threads = [f"deletion_proposal:{pid}" for pid in ids]
        for (text,) in (db.query(M.content).filter(M.user_id == user_id, M.role == "user",
                                                   M.thread_id.in_(threads))
                        .order_by(M.id.desc()).limit(12)):
            t = " ".join((text or "").split())
            if _QUOTE_MIN <= len(t) <= _QUOTE_MAX and not t.endswith("?"):
                out.append({"title": title, "text": t})
                break
        if len(out) >= _QUOTES:
            break
    return out


# ── style: fixed, never fatal ───────────────────────────────────────────────

_FIX = {"en": [(r"\b(?:truly|ultimately|incredibly)\s+", ""), (r"\bultimate\s+", ""),
               (r"\bit'?s clear that\s+", ""), (r"\bit is clear that\s+", ""),
               (r"\bhere'?s to\b", "to"), (r"\bbuckle up[,.]?\s*", ""),
               (r"\bin conclusion,?\s*", ""), (r"\bwhether you'?re\b", "if you're"),
               (r"\bjourney\b", "run"), (r"\btapestry\b", "mix"), (r"\beclectic\b", "wide"),
               (r"\bdelves?\b", "digs"), (r"\btestament\b", "proof"), (r"\bembark(?:ed)?\b", "started"),
               (r"\brealm\b", "world"), (r"\brollercoaster\b", "swing"),
               (r"\benthusiast\b", "fan"), (r"\baficionado\b", "regular")],
        "de": [(r"\bwahrlich\s+", ""), (r"\bwirklich\s+", ""), (r"\beine Reise\b", "ein Lauf"),
               (r"\bFazit:?\s*", ""), (r"\bzweifellos\s+", "")]}
_MASK = "{}"


def _mask(text: str, names: list) -> tuple:
    """Titles and artist names out of the way of the style fixes."""
    kept = []
    for n in sorted({n for n in names if n and len(n) >= 3}, key=len, reverse=True):
        pat = re.compile(rf"(?<!\w){re.escape(n)}(?!\w)" if n[-1].isalnum() else re.escape(n))

        def keep(m):
            kept.append(m.group(0))
            return _MASK.format(len(kept) - 1)
        text = pat.sub(keep, text)
    return text, kept


def _unmask(text: str, kept: list) -> str:
    return re.sub("(\\d+)", lambda m: kept[int(m.group(1))], text)


def fix_style(text: str, names: list, lang: str = "en") -> str:
    masked, kept = _mask(text or "", names)
    for pat, rep in _FIX.get(lang, []):
        masked = re.sub(pat, rep, masked, flags=re.I)
    masked = re.sub(r"!+", ".", masked)
    masked = re.sub(r"\s{2,}", " ", masked).strip()
    if masked and masked[0].islower():
        masked = masked[0].upper() + masked[1:]
    return _unmask(masked, kept)


# ── the evidence a sentence may stand on ────────────────────────────────────

_NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*")
_CITE = re.compile(r"\[((?:[IU]\d{1,3})(?:\s*,\s*[IU]\d{1,3})*)\]")
_SENT = re.compile(r"(?<=[.?])\s+(?=\S)")
_SECTION = re.compile(r"^##\s*(I\d{2}|ROAST|TOAST|NEXT)\s*(?:\|\s*(.*?))?\s*$", re.M)


def _norm(n: str) -> str:
    if re.fullmatch(r"\d{1,3}(,\d{3})+", n):
        n = n.replace(",", "")
    n = n.replace(",", ".")
    return n.split(".")[0] if re.fullmatch(r"\d+\.0+", n) else n


def numerals(text: str) -> set:
    return {_norm(n) for n in _NUM.findall(text or "")}


def evidence(cards: list, picks: dict) -> dict:
    """{id: {"numbers": set, "titles": set}} for every card and pick."""
    out = {}
    for c in cards:
        blob = " ".join([c["claim"], *[str(v) for v in c["values"].values()],
                         *[r for v in c["say"].values() for r in v], *c["refs"]])
        out[c["id"]] = {"numbers": numerals(blob), "titles": {r.lower() for r in c["refs"]}}
    for uid, p in picks.items():
        blob = " ".join(str(x) for x in (p.get("title"), p.get("year"), p.get("reason")) if x)
        out[uid] = {"numbers": numerals(blob), "titles": {(p.get("title") or "").lower()}}
    return out


def _small() -> set:
    return {str(n) for n in range(0, 13)}


def _sentences(text: str) -> list:
    """Sentences with the citations each rests on. A sentence without its
    own citation takes the next cited sentence's — models often cite once at
    the end of a two-sentence thought — and trailing ones take none here
    (the caller falls back to the section's own card)."""
    out, pending = [], []
    for sent in (s for s in _SENT.split(text.strip()) if s.strip()):
        cites = [c.strip() for m in _CITE.findall(sent) for c in m.split(",")]
        if cites:
            out += [(p, cites) for p in pending] + [(sent, cites)]
            pending = []
        else:
            pending.append(sent)
    return out + [(p, []) for p in pending]


def check_section(text: str, headline: str, own: list, ev: dict, known_titles: list) -> list:
    """The faults of one written section. Strict on truth: each sentence's
    numerals must be its cited cards' (or the section's own card's), and a
    library title it names must be on one of them. ``own``: the ids the
    section is about (its card, or the pick)."""
    faults = []
    if not text.strip():
        return ["the section is empty"]
    if len(text) > _TEXT_MAX:
        faults.append(f"too long ({len(text)} characters, at most {_TEXT_MAX})")
    allowed_own = set().union(*(ev[i]["numbers"] for i in own if i in ev)) | _small()
    for n in numerals(headline) - allowed_own:
        faults.append(f"the headline's number {n} is not on the card")
    for sent, cites in _sentences(text):
        cites = cites or [i for i in own if i in ev]
        if not cites:
            faults.append(f"no citation: \"{sent[:80]}\"")
            continue
        unknown = [c for c in cites if c not in ev]
        if unknown:
            faults.append(f"cites {', '.join(unknown)}, which is not a card")
            continue
        bare = _CITE.sub("", sent)
        allowed = set().union(*(ev[c]["numbers"] for c in cites)) | _small()
        for n in numerals(bare) - allowed:
            faults.append(f"the number {n} is not on {', '.join(cites)}: \"{bare.strip()[:80]}\"")
        titles = set().union(*(ev[c]["titles"] for c in cites)) | set().union(
            *(ev[i]["titles"] for i in own if i in ev))
        low = bare.lower()
        for t in known_titles:
            tl = t.lower()
            if len(tl) >= 4 and tl not in titles and re.search(rf"(?<!\w){re.escape(tl)}(?!\w)", low):
                faults.append(f"names {t} without citing the card that has it")
    return faults


def tuck_cites(text: str) -> str:
    """"…hours. [I03]" → "…hours [I03]." — a citation written after the full
    stop belongs to the sentence before it."""
    return re.sub(r"([.?;])\s*(\[[IU]\d{1,3}(?:\s*,\s*[IU]\d{1,3})*\])", r" \2\1", text or "")


def strip_cites(text: str) -> str:
    return re.sub(r"\s+([.,;?])", r"\1", _CITE.sub("", text)).strip()


def parse_sections(raw: str) -> dict:
    """{"I03": (headline, text), "ROAST": ("", text), …} from the writer."""
    out, marks = {}, list(_SECTION.finditer(raw or ""))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
        out[m.group(1)] = ((m.group(2) or "").strip()[:_HEAD_MAX], raw[m.end():end].strip())
    return out


# ── the editor ──────────────────────────────────────────────────────────────

def editor_schema(n_cards: int) -> dict:
    card = {"type": "string", "pattern": r"^I\d{2}$"}
    return {"type": "object", "properties": {
        "arc": {"type": "array", "items": card, "minItems": min(_ARC_MIN, n_cards),
                "maxItems": _ARC_MAX},
        "roast": card, "toast": card,
        "archetype_trait": {"type": "string"},
        "archetype_title": {"type": "string", "maxLength": 44},
        "archetype_subhead": {"type": "string", "maxLength": 160},
        "share_line": {"type": "string", "maxLength": 110},
        "next_pick": {"type": "string"},
        "cluster_names": {"type": "array", "items": {"type": "object", "properties": {
            "ref": {"type": "string"}, "name": {"type": "string", "maxLength": 36}},
            "required": ["ref", "name"]}, "maxItems": 3}},
        "required": ["arc", "roast", "toast", "archetype_trait", "archetype_title",
                     "archetype_subhead", "share_line", "next_pick"]}


def default_edit(cards: list, traits: list, picks: dict) -> dict:
    """The editor's choice made by rule: the top cards in beat order, the
    strongest confession or twist as the roast, the strongest identity or
    superlative as the toast."""
    from src.services.wrapup_story import TRAIT_TITLES
    order = list(BEAT_HEADLINES)
    top = sorted(cards[:_ARC_MAX], key=lambda c: (order.index(c["slide"]) if c["slide"] in order else 99,
                                                  -c["score"]))
    roast = next((c["id"] for c in cards if c["kind"] in ("confession", "contrast")), cards[0]["id"])
    toast = next((c["id"] for c in cards if c["kind"] in ("identity", "superlative") and c["id"] != roast),
                 cards[-1]["id"])
    key = traits[0] if traits else None
    title, sub = TRAIT_TITLES.get(key, ("Your Period in Review", "The numbers, without the commentary."))
    return {"arc": [c["id"] for c in top], "roast": roast, "toast": toast, "archetype_trait": key,
            "archetype_title": title, "archetype_subhead": sub, "share_line": title,
            "next_pick": next(iter(picks), ""), "cluster_names": []}


def check_edit(e: dict, cards: list, traits: list, picks: dict, clusters: dict) -> list:
    ids = {c["id"] for c in cards}
    faults = []
    arc = e.get("arc") or []
    if len(arc) < min(_ARC_MIN, len(ids)) or len(set(arc)) != len(arc) or not set(arc) <= ids:
        faults.append(f"arc must be {min(_ARC_MIN, len(ids))}-{_ARC_MAX} distinct card ids")
    for k in ("roast", "toast"):
        if e.get(k) not in ids:
            faults.append(f"{k} must be a card id")
    if traits and e.get("archetype_trait") not in traits:
        faults.append(f"archetype_trait must be one of {', '.join(traits)}")
    if e.get("next_pick") and e["next_pick"] not in picks:
        faults.append("next_pick must be a U# pick or empty")
    if any(cn.get("ref") not in clusters for cn in e.get("cluster_names") or []):
        faults.append("cluster_names may only name the C# clusters")
    return faults


# ── the brief ───────────────────────────────────────────────────────────────

def _card_line(c: dict) -> str:
    say = "; ".join(f"{k}: " + " | ".join(v) for k, v in c["say"].items() if v)
    return (f"{c['id']} [{c['slide']}] {c['claim']} — why: {c['why']}."
            + (f" Say: {say}." if say else ""))


EDITOR_SYSTEM = """You are Curatarr's editor, planning ONE person's wrap-up from ranked insight
cards. You choose; you do not write the story yet.
- arc: 3-7 card ids in the order the story should tell them — open with a number, end on
  who they were. Prefer high scores; one card per beat is plenty.
- roast: the card that shows a blind spot or a contradiction. toast: the card that shows
  their strongest taste. Either may also be in the arc.
- archetype_trait: one of the TRAITS. archetype_title: 2-4 words, a role they earned from
  it, concrete and a little pointed ("The Midnight Completionist"); never Enthusiast, Lover,
  Fan, Buff, Aficionado. archetype_subhead: one sentence that earns it from the cards.
- share_line: under 100 characters, something they would post.
- next_pick: the U# pick to end on, or "" when there is none.
- cluster_names: a 2-4 word name for each C# taste cluster, if any.
Answer with the JSON object only."""

WRITER_SYSTEM = """You are Curatarr, the household's media curator, writing ONE person's wrap-up
in {language}. You know them from months of arguing about what stays on the server. Voice:
a sharp critic friend — dry, specific, affectionate underneath. Never a greeting card.

Write one section per card in the PLAN, in that order, then ROAST, TOAST and NEXT:

## I03 | <headline, under 60 characters>
<2-3 sentences>

## ROAST
<1-2 sentences: the blind spot the roast card proves>
## TOAST
<1-2 sentences: their best taste, with respect>
## NEXT
<1-2 sentences: the challenge, around the next pick>

RULES
- End each sentence (or each two-sentence thought) with the cards it rests on in
  brackets: [I03] or [I03, I07] or [U1].
- Numbers: only the cited cards' own, written as the card writes them or as one of its
  "Say" renderings. Never compute one.
- Name a title only when a cited card has it.
- Wit comes from putting two real facts side by side, not from adjectives.
- If YOUR WORDS are given, you may quote one back to them where it fits — exactly, in quotes.
No preamble, no closing remark: only the sections."""


def build_writer_brief(plan: list, cards_by_id: dict, edit: dict, picks: dict,
                       quotes: list, sheet: str) -> str:
    lines = ["PLAN (write these, in order):"]
    lines += [_card_line(cards_by_id[i]) for i in plan]
    for k in ("roast", "toast"):
        lines.append(f"{k.upper()} card: {_card_line(cards_by_id[edit[k]])}")
    p = picks.get(edit.get("next_pick") or "")
    if p:
        lines.append(f"NEXT pick {edit['next_pick']}: {p['title']}"
                     + (f" ({p['year']})" if p.get("year") else "")
                     + (f" — {p['reason']}" if p.get("reason") else ""))
    if quotes:
        lines.append("YOUR WORDS (things they told you in debates; quote at most one, exactly):")
        lines += [f"- about {q['title']}: \"{q['text']}\"" for q in quotes]
    lines.append(f"ARCHETYPE: {edit['archetype_title']} — {edit['archetype_subhead']}")
    lines.append("\nBACKGROUND FACTS (for context; cite only cards):\n" + sheet)
    return "\n".join(lines)


# ── the model calls ─────────────────────────────────────────────────────────

async def _ask(messages: list, *, fmt=None, temperature: float, predict: int) -> str:
    from src.config import settings
    from src.services.llm_errors import post_chat
    from src.services.llm_priority import background_curator_call
    from src.services.llm_utils import curator_payload
    from src.services.ollama_hosts import host_for
    host = host_for("curator")
    model = settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL
    payload = curator_payload(messages, model=model, temperature=temperature, num_predict=predict,
                              fmt=fmt, repeat_penalty=1.0 if fmt else 1.1, host=host)
    data = await background_curator_call(
        "wrap-up", lambda: post_chat(host.url, payload, read_timeout=_TIMEOUT))
    return (data.get("message") or {}).get("content", "") or ""


def _model_name() -> str:
    from src.config import settings
    return settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL


# ── the pipeline ────────────────────────────────────────────────────────────

def _template(section: str, card: Optional[dict], pick: Optional[dict]) -> tuple:
    if section == "NEXT":
        if not pick:
            return "", ""
        why = f" {pick['reason'].rstrip('.')}." if pick.get("reason") else ""
        return "", f"{pick['title']} is on the server and you have not played it yet.{why}"
    head = BEAT_HEADLINES.get(card["slide"], "") if section.startswith("I") else ""
    why = card["why"][:1].upper() + card["why"][1:]
    return head, f"{card['claim']} {why}."


async def write(facts: dict, sheet: str, idx: dict, cards: list, *, quotes: list = (),
                lang: str = "en") -> tuple:
    """(story dict, source, model) — source llm / llm_repaired / fallback.
    Never raises on a bad answer: what fails falls back slide by slide."""
    import json
    from src.services.llm_errors import LLMCallError
    from src.services.llm_utils import parse_llm_json

    picks = idx.get("picks") or {}
    traits = idx.get("traits") or []
    clusters = idx.get("clusters") or {}
    cards_by_id = {c["id"]: c for c in cards}
    known_titles = sorted({v["title"] for v in (idx.get("items") or {}).values()}
                          | {p["title"] for p in picks.values()}, key=len, reverse=True)
    model = _model_name()
    if not cards:
        return None, "nothing worth saying", model

    # 1. the editor
    edit, edit_source = None, "fallback"
    try:
        raw = await _ask([{"role": "system", "content": EDITOR_SYSTEM},
                          {"role": "user", "content": "CARDS:\n" + "\n".join(_card_line(c) for c in cards)
                           + (f"\nTRAITS: {', '.join(traits)}" if traits else "")
                           + (f"\nPICKS: " + "; ".join(f"{k} {v['title']}" for k, v in picks.items())
                              if picks else "")
                           + (f"\nCLUSTERS: " + "; ".join(f"{k} around {', '.join(v.get('top_titles') or [])}"
                                                          for k, v in clusters.items()) if clusters else "")}],
                         fmt=editor_schema(len(cards)), temperature=_TEMP_EDITOR, predict=_PREDICT_EDITOR)
        cand = parse_llm_json(raw)
        if isinstance(cand, dict) and not check_edit(cand, cards, traits, picks, clusters):
            edit, edit_source = cand, "llm"
        else:
            logger.info("[wrapup] editor answer unusable: %s",
                        check_edit(cand, cards, traits, picks, clusters) if isinstance(cand, dict) else "no JSON")
    except LLMCallError as e:
        logger.info("[wrapup] editor unavailable: %s", e)
        return None, f"curator unavailable: {e}", model
    except (ValueError, TypeError) as e:
        logger.info("[wrapup] editor answer was not JSON: %s", e)
    edit = edit or default_edit(cards, traits, picks)
    names = known_titles
    allowed_head = set().union(*(numerals(_card_line(c)) for c in cards)) | _small()
    for k in ("archetype_title", "archetype_subhead", "share_line"):
        edit[k] = fix_style(edit.get(k) or "", names, lang)
        if numerals(edit[k]) - allowed_head:
            edit[k] = default_edit(cards, traits, picks)[k]

    sections = [*edit["arc"], "ROAST", "TOAST"] + (["NEXT"] if edit.get("next_pick") else [])
    own = {s: [s] for s in edit["arc"]}
    own.update({"ROAST": [edit["roast"]], "TOAST": [edit["toast"]],
                "NEXT": [edit["next_pick"]] if edit.get("next_pick") else []})
    ev = evidence(cards, picks)
    brief = build_writer_brief(edit["arc"], cards_by_id, edit, picks, list(quotes), sheet)
    system = WRITER_SYSTEM.format(language=LANG_NAMES.get(lang, "English"))

    def judge(parsed: dict) -> dict:
        out = {}
        for s in sections:
            head, text = parsed.get(s, ("", ""))
            text = fix_style(tuck_cites(text), names, lang)
            head = fix_style(head, names, lang)
            faults = check_section(text, head, own[s], ev, known_titles) if text else ["missing"]
            out[s] = (head, text, faults)
        return out

    # 2. the writer
    written, sources = {}, {}
    try:
        raw = await _ask([{"role": "system", "content": system}, {"role": "user", "content": brief}],
                         temperature=_TEMP_WRITER, predict=_PREDICT_WRITER)
        judged = judge(parse_sections(raw))
        bad = {s: f for s, (_, _, f) in judged.items() if f}
        for s, (h, t, f) in judged.items():
            if not f:
                written[s], sources[s] = (h, t), "llm"
        # 3. one repair turn for the slides that failed, every fault named
        if bad:
            logger.info("[wrapup] %d section(s) back for repair: %s", len(bad), bad)
            ask = "These sections need fixing. Send ONLY these sections again, every rule kept:\n" + "\n".join(
                f"## {s}: " + "; ".join(f) for s, f in bad.items())
            raw2 = await _ask([{"role": "system", "content": system}, {"role": "user", "content": brief},
                               {"role": "assistant", "content": raw[:4000]},
                               {"role": "user", "content": ask}],
                              temperature=_TEMP_WRITER, predict=_PREDICT_REPAIR)
            judged2 = judge(parse_sections(raw2))
            for s in bad:
                h, t, f = judged2[s]
                if not f:
                    written[s], sources[s] = (h, t), "repaired"
    except LLMCallError as e:
        logger.info("[wrapup] writer unavailable: %s", e)
    # 4. the template, for whatever is still missing
    for s in sections:
        if s not in written:
            card = cards_by_id.get(own[s][0]) if own[s] and own[s][0] in cards_by_id else None
            pick = picks.get(own[s][0]) if own[s] else None
            written[s], sources[s] = _template(s, card, pick), "template"

    story = assemble(edit, written, sources, cards_by_id, idx)
    vals = set(sources.values())
    if vals == {"template"}:
        source = "fallback"
    elif vals == {"llm"} and edit_source == "llm":
        source = "llm"
    else:
        source = "llm_repaired"
    logger.info("[wrapup] story written: %s", json.dumps(Counter(sources.values())))
    return story, source, model


def assemble(edit: dict, written: dict, sources: dict, cards_by_id: dict, idx: dict) -> dict:
    """The v2 story — slides — plus the v1 shape the current page reads
    (chapters, roast and toast, the hook), so both render from one answer."""
    by_title = {v["title"].lower(): k for k, v in (idx.get("items") or {}).items()}

    def trefs(card):
        return [by_title[r.lower()] for r in (card or {}).get("refs", []) if r.lower() in by_title]

    slides = []
    for cid in edit["arc"]:
        head, text = written[cid]
        card = cards_by_id[cid]
        slides.append({"id": cid, "beat": card["slide"], "kind": card["kind"], "headline": head,
                       "text": strip_cites(text), "item_refs": trefs(card), "source": sources[cid],
                       "score": card["score"]})
    roast, toast = strip_cites(written["ROAST"][1]), strip_cites(written["TOAST"][1])
    nxt = strip_cites(written.get("NEXT", ("", ""))[1]) if "NEXT" in written else ""
    return {
        "version": 2,
        "archetype_title": edit["archetype_title"], "archetype_subhead": edit["archetype_subhead"],
        "archetype_trait": edit.get("archetype_trait"), "share_line": edit["share_line"],
        "slides": slides,
        "slide_sources": sources,
        "cluster_names": [cn for cn in edit.get("cluster_names") or []
                          if cn.get("ref") in (idx.get("clusters") or {})],
        # the v1 shape, derived
        "era_chapters": [{"title": s["headline"], "kicker": cards_by_id[s["id"]]["why"][:110],
                          "commentary": s["text"], "item_refs": s["item_refs"], "fact_refs": []}
                         for s in slides],
        "roast_and_toast": {"roast": roast, "toast": toast} if roast and toast else None,
        "look_ahead_hook": ({"challenge": nxt, "item_ref": edit["next_pick"], "fact_refs": []}
                            if nxt else None),
    }
