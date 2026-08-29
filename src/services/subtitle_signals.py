"""Execution signals derived from a title's subtitle track.

The judge reasons only from metadata about a work — synopsis, genres, ratings,
awards, significance. It has never seen a line of the work itself. That creates
a contradiction we built ourselves: the constitution DEMANDS the premise /
execution distinction ("a work whose premise CLAIMS the qualities the owner
rewards but whose EXECUTION is generic does NOT pass"), while the no-invention
rule FORBIDS any execution verdict without evidence. It is asked to judge
exactly what it is blind to. Hardest hit is the RESONANCE pillar, whose litmus
asks whether a work "hums rather than screams" — with no rhythm evidence at all.

This module turns a subtitle track into a few cheap numbers that speak to that.
It is deliberately small, deterministic and LLM-free.

WHAT THESE NUMBERS ARE NOT
--------------------------
They describe the SUBTITLE TRACK, not the work:

* Subtitles are condensed BY DESIGN. Professional style guides cap reading
  speed (Netflix: 20 characters/second for adults) and instruct authors to
  "favor text reduction, deletion and condensing". A word count therefore
  systematically UNDERCOUNTS spoken dialogue.
* A track may be a translation, in which case its rhythm is the translator's
  as much as the writer's.
* Timestamps drift — frame-rate conversion, regional cuts, ad-break retiming.
* Forced tracks carry only foreign lines and signage; measuring one would
  report near-silence for a talkative film. They are detected and refused.
* SDH tracks add bracketed sound cues, ALL-CAPS signage and ♪-marked lyrics —
  non-dialogue tokens that are stripped before anything is counted.

Reported correlations between dialogue density and anything meaningful are real
but weak (published genre classifiers reach ~0.7 F1). So: these are INDICATIVE,
never decisive, and every consumer of them must say so. The measured spread on
this library is nonetheless large enough to matter — 42 words/min for a
contemplative drama against 125 for a dialogue piece.

DELIBERATELY ABSENT: sentiment "narrative arcs". The six-basic-shapes result
that popularised them was shown to be a low-pass-filter artifact of the
method (Swafford's critique, conceded by the tool's own author). We do not
build on a debunked method.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

logger = logging.getLogger(__name__)

# Bump when a formula below changes, so stamped rows re-offer themselves.
METRICS_VERSION = "1"

# A dialogue gap longer than this counts as "no dialogue". Twenty seconds is
# long enough that ordinary conversational pauses never qualify, short enough
# that a genuinely wordless sequence registers.
_SILENCE_GAP_S = 20.0

# Below this, a track cannot be a full dialogue transcript for a feature — a
# forced/signage track typically lands in the tens.
_MIN_CUES = 120

# Cues must span at least this fraction of the runtime. A forced track clusters
# around the few foreign-language scenes and fails here even when it has many
# cues; a real track covers the whole film (measured: 94-98% on real files).
_MIN_COVERAGE = 0.5

# ...and no more than this. The check was one-sided at first, which let a
# genuinely wrong file through: a 22-minute episode was measured against a
# subtitle spanning 52 minutes (coverage 2.34), and every word in it was
# divided by the episode's runtime — 243 words/min, nearly double anything
# else in the library, from a file that simply belonged to something else.
# A little slack above 1.0 is normal (end cards, timing drift, a track that
# runs past the last frame); 30% is not.
_MAX_COVERAGE = 1.3

_TS = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_TAGS = re.compile(r"<[^>]+>|\{\\[^}]*\}")           # HTML + ASS override tags
_BRACKETED = re.compile(r"\[[^\]]*\]|\([^)]*\)")      # [door slams] / (sighs)
_LYRIC = re.compile(r"[♪♫#]+[^\n]*")
_SPEAKER = re.compile(r"^\s*[-–—]?\s*[A-ZÄÖÜ][A-ZÄÖÜ .'-]{1,24}:\s*", re.M)
_ALLCAPS_LINE = re.compile(r"^[^a-zäöüß\n]{8,}$", re.M)
# Apostrophes stay INSIDE the word: splitting "don't" into two tokens
# inflates English tracks against German ones, and the whole point of a
# words-per-minute figure is that it is comparable across a library.
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)

# ASS/SSA. Anime subtitles are overwhelmingly this format, and it is not SRT
# with different punctuation: events are comma-separated records whose field
# ORDER is declared by a "Format:" line, timestamps run H:MM:SS.cc, and a
# single file routinely carries several parallel tracks — the dialogue, the
# translated signs, and karaoke for the opening. Measuring the signs track
# would report a talkative episode as nearly silent, the same failure mode
# forced tracks cause, so styles are filtered by name.
_ASS_EVENT = re.compile(r"^(Dialogue|Comment)\s*:\s*(.*)$", re.M | re.I)
_ASS_FORMAT = re.compile(r"^Format\s*:\s*(.+)$", re.M | re.I)
_ASS_TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})[.,](\d{1,3})$")
# Style names that mark a non-dialogue layer. A negative list on purpose:
# dialogue styles are named anything ("Default", "Main", "Deutsch"), while
# the extras are named remarkably consistently across fansub groups.
# EVERY alternative is word-bounded, and that is not cosmetic: an unbounded
# "title" matches "Subtitles" — the single most common name a dialogue style
# has — and would silently discard the entire dialogue of any file that used
# it, leaving too few cues to pass the forced-track check. The result would
# have been a talkative episode reported as having no subtitles at all. Same
# trap for "sign" inside "Design". (Found on the serving side first; the same
# pattern was in here.)
_ASS_NON_DIALOGUE = re.compile(
    r"\bsigns?\b|\bsongs?\b|\bkaraoke?\b|\blyrics?\b|\bop\b|\bed\b|"
    r"\bopening\b|\bending\b|\bcredits?\b|\btitles?\b|\btypeset\w*\b|"
    r"\bnotes?\b|\bcaptions?\b|\bstaff\b", re.I)
# Vector drawing commands ({\p1}m 0 0 l 100 0 …) are graphics, not words.
_ASS_DRAWING = re.compile(r"\{[^}]*\\p[1-9][^}]*\}[^{]*")
_ASS_BREAKS = re.compile(r"\\[Nnh]")


# Subtitle formats that actually contain TEXT. Deliberately a positive list:
# VobSub, PGS and DVB subtitles are BITMAPS — pictures of words, megabytes
# each, with nothing to parse. Measured on a real service, those were exactly
# the 18 MB "subtitles" that dominated download time. Plex exposes them too
# (pgs, vobsub, eia_608 all appear in this library), and while none of them
# happens to carry a download key today, that is a property of the library,
# not a guarantee.
_TEXT_CODECS = {"srt", "subrip", "ass", "ssa", "vtt", "webvtt", "mov_text",
                "text", "utf-8", "microdvd", "subviewer"}

# Markers in a track's own name that say it is not plain dialogue.
_SDH_NAME = re.compile(r"\bsdh\b|hearing[ _-]?impaired|\bcc\b", re.I)
_FORCED_NAME = re.compile(r"\bforced\b|\bsigns?\b|songs?\s*&?\s*signs?", re.I)


def _ass_seconds(v: str):
    """H:MM:SS.cc -> seconds. ASS counts CENTIseconds, not milliseconds."""
    m = _ASS_TIME.match((v or "").strip())
    if not m:
        return None
    h, mi, sec, frac = m.groups()
    scale = 100.0 if len(frac) <= 2 else 1000.0
    return int(h) * 3600 + int(mi) * 60 + int(sec) + int(frac) / scale


def parse_ass(text: str) -> list:
    """Parse the dialogue events of an ASS/SSA file.

    Field order is read from the section's own ``Format:`` line rather than
    assumed — groups do reorder it, and a hardcoded index would silently read
    the style name as a timestamp. ``Comment:`` events are skipped (they are
    not rendered), as are styles whose name marks them as signs, karaoke or
    credits.
    """
    out = []
    fields = ["layer", "start", "end", "style", "name", "marginl", "marginr",
              "marginv", "effect", "text"]
    fm = _ASS_FORMAT.search(text or "")
    if fm:
        parsed = [f.strip().lower() for f in fm.group(1).split(",")]
        if "start" in parsed and "text" in parsed:
            fields = parsed
    i_start, i_end = fields.index("start"), fields.index("end")
    i_text = fields.index("text")
    i_style = fields.index("style") if "style" in fields else None

    for kind, body in _ASS_EVENT.findall(text or ""):
        if kind.lower() != "dialogue":
            continue
        # The text field itself may contain commas, so the split is bounded.
        parts = body.split(",", len(fields) - 1)
        if len(parts) <= max(i_start, i_end, i_text):
            continue
        start = _ass_seconds(parts[i_start])
        end = _ass_seconds(parts[i_end])
        if start is None or end is None or end < start:
            continue
        if i_style is not None and _ASS_NON_DIALOGUE.search(parts[i_style]):
            continue
        body_txt = _ASS_DRAWING.sub(" ", parts[i_text])
        body_txt = _ASS_BREAKS.sub(" ", body_txt)
        # A cue that was ONLY a vector drawing carries no dialogue; keeping it
        # would count as speech for the silence arithmetic.
        if not _TAGS.sub(" ", body_txt).strip():
            continue
        out.append((start, end, body_txt.strip()))
    out.sort(key=lambda c: c[0])
    return out


def looks_binary(text: str) -> bool:
    """True when this is a picture-based subtitle, not text.

    Bitmap formats (VobSub, PGS) declare themselves badly and travel under
    the same names as text ones, so the content decides: NUL bytes and
    replacement characters do not occur in a real subtitle file, and a track
    with almost no letters cannot be dialogue. Cheap enough to run on every
    fetched file, and it protects every source at once — the local sidecar,
    an operator's service, and the public fallback.
    """
    if not text:
        return True
    head = text[:4000]
    if "\x00" in head or head.count("\ufffd") > 20:
        return True
    # Tab, newline and carriage return are the only control codes a
    # subtitle file has any business containing.
    ctrl = sum(1 for c in head if ord(c) < 32 and ord(c) not in (9, 10, 13))
    if ctrl > len(head) * 0.02:
        return True
    # The soft signal, and only where it means something: a SHORT file may
    # be mostly timestamps and still be perfectly valid (a three-cue VTT is
    # 23% letters), so the letter ratio is judged on substantial files only.
    if len(head) < 500:
        return False
    letters = sum(1 for c in head if c.isalpha())
    return letters < len(head) * 0.12


def parse_cues(text: str) -> list[tuple[float, float, str]]:
    """Parse SRT/VTT into ``(start_s, end_s, text)``.

    Hand-rolled on purpose: the format is two regexes' worth of work, and
    requirements.txt is a short pinned list the owner installs by hand — a
    dependency for this would cost more than it saves. Malformed blocks are
    skipped rather than raised on; real-world subtitle files are messy and a
    single bad timestamp must not lose the other 1,400 cues.
    """
    out: list[tuple[float, float, str]] = []
    if not text or looks_binary(text):
        return out
    # ASS/SSA is a different container entirely — anime subtitles are almost
    # always this, and its events carry no "-->" for the SRT path to find.
    if "[Events]" in text or _ASS_EVENT.search(text):
        return parse_ass(text)
    # Normalise line endings; VTT differs from SRT only in a header and dots.
    blocks = re.split(r"\r?\n\r?\n+", text.replace("\r\n", "\n").strip())
    for block in blocks:
        m = _TS.search(block)
        if not m:
            continue
        try:
            h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(g) for g in m.groups())
        except (TypeError, ValueError):
            continue
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
        end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
        if end < start:
            continue
        body = block[m.end():].strip("\n")
        # Drop a leading cue number if it survived the split.
        body = re.sub(r"^\s*\d+\s*\n", "", body)
        out.append((start, end, body.strip()))
    return out


def strip_non_dialogue(text: str) -> str:
    """Remove everything in a cue that is not spoken dialogue.

    Uploader tagging is unreliable, so this runs on every track regardless of
    how it was labelled: bracketed sound cues, ♪-marked lyrics, ALL-CAPS
    signage lines, speaker prefixes ("MARY: ") and markup. Measured effect on
    a real SDH track: 98 such markers, all removed; a plain track loses nothing.
    """
    if not text:
        return ""
    t = _ASS_BREAKS.sub(" ", text)
    t = _TAGS.sub(" ", t)
    t = _LYRIC.sub(" ", t)
    t = _BRACKETED.sub(" ", t)
    t = _ALLCAPS_LINE.sub(" ", t)
    t = _SPEAKER.sub("", t)
    return t


def count_sdh_markers(text: str) -> int:
    """How many non-dialogue markers the RAW text carries — the signal that a
    track is SDH even when nothing in its name says so."""
    if not text:
        return 0
    return (len(_BRACKETED.findall(text))
            + len(_LYRIC.findall(text))
            + len(_ALLCAPS_LINE.findall(text)))


# Scripts that do not put spaces between words. Counting whitespace-separated
# "words" in any of these measures the writing system, not the dialogue: a
# real Thai episode track came back as 480 cues carrying 74 "words", which
# read as an almost silent film. CJK was covered from the start; Thai, Lao,
# Khmer, Burmese and Tibetan work the same way and were not.
_NO_WORD_BREAK = ("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "THAI", "LAO",
                  "KHMER", "MYANMAR", "TIBETAN")


def script_of(text: str) -> str:
    """``latin`` / ``unspaced`` / ``other`` — decided from the TEXT, never a tag.

    Language tags on real files are not trustworthy: this library carries a
    track tagged ``hi`` (Hindi) containing no Devanagari at all, and one tagged
    ``ja`` whose whitespace-separated word count would be impossible for real
    Japanese. It matters because Chinese/Japanese/Korean do not separate words
    with spaces — a words-per-minute figure computed there is meaningless, so
    the caller must be able to refuse it.
    """
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return "other"
    unspaced = 0
    for c in letters[:4000]:
        try:
            name = unicodedata.name(c)
        except ValueError:
            continue
        if any(marker in name for marker in _NO_WORD_BREAK):
            unspaced += 1
    return "unspaced" if unspaced / min(len(letters), 4000) > 0.15 else "latin"


def tokenize(text: str) -> list[str]:
    """Lowercased word tokens. Digits and punctuation are not words."""
    return [w.lower() for w in _WORD.findall(text or "")]


def mattr(tokens: list[str], window: int = 100) -> Optional[float]:
    """Moving-Average Type-Token Ratio — lexical diversity that survives a
    length comparison.

    Plain type-token ratio CANNOT be used here: vocabulary grows sublinearly
    while tokens grow linearly, so raw TTR falls purely as a text gets longer
    and a 220-minute epic would look "less varied" than a short film by
    construction. MATTR averages the ratio over a fixed-size sliding window,
    which removes that dependence. (MTLD is the other validated correction;
    MATTR is chosen for being deterministic and small enough to verify by eye.)

    Returns ``None`` below one window's worth of tokens — a figure from less
    than that is not comparable to anything.
    """
    if not tokens or len(tokens) < window:
        return None
    total = 0.0
    n = 0
    for i in range(len(tokens) - window + 1):
        total += len(set(tokens[i:i + window])) / window
        n += 1
    return round(total / n, 3) if n else None


def looks_forced(cues: list, duration_min: float) -> bool:
    """True when a track cannot be a full dialogue transcript.

    Forced tracks render only foreign lines and signage. Measuring one would
    report a talkative film as nearly silent — worse than having no number at
    all. Two independent tests: too few cues, or cues that do not span the
    runtime (real tracks covered 94-98% of it on every file measured).
    """
    if not cues:
        return True
    if len(cues) < _MIN_CUES:
        return True
    if duration_min and duration_min > 0:
        span_min = (cues[-1][1] - cues[0][0]) / 60.0
        cov = span_min / duration_min
        if cov < _MIN_COVERAGE or cov > _MAX_COVERAGE:
            return True
    return False


def subtitle_metrics(cues: list, duration_min: float,
                     *, track_name: str = "") -> dict:
    """Reduce parsed cues to the numbers the judge sees. Never raises.

    Returns ``{}`` when the track is unusable (forced, empty, or a script
    without word boundaries) — the caller stamps that as "checked, nothing
    here" rather than retrying forever.
    """
    if not cues or not duration_min or duration_min <= 0:
        return {}
    if looks_forced(cues, duration_min):
        return {}

    raw = "\n".join(c[2] for c in cues)
    sdh_markers = count_sdh_markers(raw)
    clean = strip_non_dialogue(raw)
    script = script_of(clean)
    if script == "unspaced":
        # No whitespace word boundaries — a words-per-minute figure would be an
        # artifact of the writing system, not of the film.
        return {}

    toks = tokenize(clean)
    if not toks:
        return {}
    # Second line of defence, for scripts the check above does not know: a
    # real dialogue track averages several words per cue (measured: 4-8 on
    # this library). A fraction of a word per cue means the text is not
    # being tokenised as language at all — or the track is signage.
    if len(toks) / len(cues) < 1.0:
        return {}

    gaps = 0.0
    for (s1, e1, _), (s2, _e2, _t) in zip(cues, cues[1:]):
        gap = s2 - e1
        if gap > _SILENCE_GAP_S:
            gaps += gap
    span_min = (cues[-1][1] - cues[0][0]) / 60.0

    name = track_name or ""
    return {
        "words_per_min": round(len(toks) / duration_min, 1),
        "silent_min": round(gaps / 60.0, 1),
        "silent_share": round(min(1.0, (gaps / 60.0) / duration_min), 3),
        "mattr": mattr(toks),
        "total_words": len(toks),
        # NOT capped at 1.0 — the capping is what hid a 2.34 mismatch behind
        # a reassuring "1.0" while the words-per-minute figure was inflated.
        "coverage": round(span_min / duration_min, 3),
        "cue_count": len(cues),
        "is_sdh": bool(_SDH_NAME.search(name)) or sdh_markers >= 25,
        "duration_min": round(duration_min, 1),
        "metrics_v": METRICS_VERSION,
    }


def format_dialogue_line(m: dict, lang: str = "") -> str:
    """The one line that reaches the judge, or "" when there is nothing to say.

    Silence is the default: a missing line is honest, a guessed one is not
    (the same rule ``size_context_for`` follows). The caveat travels WITH the
    number, unconditionally — the file-size lesson was that a bare figure in an
    evidence block gets promoted to a verdict by the model, so the sentence
    that forbids that has to sit next to it, every time.
    """
    if not m or not m.get("words_per_min"):
        return ""
    lang_part = f"{lang} " if lang else ""
    sdh = ", SDH track" if m.get("is_sdh") else ""
    div = (f", lexical diversity {m['mattr']}" if m.get("mattr") is not None
           else "")
    return (
        f"DIALOGUE: {m['words_per_min']:.0f} words/min, "
        f"{m['silent_min']:.0f} of {m['duration_min']:.0f} min without dialogue"
        f"{div} ({lang_part}subtitle track{sdh}).\n"
        f"  Describes the SUBTITLE TRACK, which is condensed by design and may "
        f"be a translation — indicative, never decisive. A low word rate means "
        f"SPARSE DIALOGUE, not a thin work: visually-driven cinema is not "
        f"penalised here.\n"
    )


# ── ACQUISITION ──────────────────────────────────────────────────────────────
# Plex sidecar only, for now. Embedded tracks are NOT reachable: the stream
# endpoint answers 501, the part endpoint 503 and the transcoder 400 (all
# measured on the live server), and this host has neither filesystem access to
# the media nor ffmpeg. That caps reach at ~70% of movies but only ~12-23% of
# series and anime — an honest gap, not a silent one: a title with no reachable
# track simply gets no line, exactly as size_context_for stays silent rather
# than guessing.

def _plex() -> tuple:
    from src.config import settings
    return settings.effective_plex_url, settings.effective_plex_token


def pick_subtitle_stream(streams: list):
    """Choose the track to measure, or None.

    Only sidecar streams (those carrying a ``key``) can be fetched at all.
    Among them, a track whose name says forced/signage is refused outright —
    measuring one would report a talkative film as nearly silent. A plain
    track is preferred over an SDH one, since SDH needs heavier stripping;
    SDH is still accepted when it is all there is.
    """
    subs = [s for s in (streams or [])
            if s.get("streamType") == 3 and s.get("key")
            and (s.get("codec") or "").lower() in _TEXT_CODECS]
    if not subs:
        return None

    def _name(s):
        return " ".join(str(s.get(k) or "") for k in
                        ("title", "displayTitle", "extendedDisplayTitle"))

    usable = [s for s in subs
              if not s.get("forced") and not _FORCED_NAME.search(_name(s))]
    if not usable:
        return None
    plain = [s for s in usable if not _SDH_NAME.search(_name(s))]
    return (plain or usable)[0]


async def fetch_subtitle_metrics(plex_rating_key: str, hints: dict = None):
    """Measure one title's dialogue. TRI-STATE, like fetch_significance:

      dict — measured numbers,
      {}   — DEFINITIVELY nothing usable (no track anywhere, forced-only, a
             script without word boundaries) -> caller may stamp checked,
      None — TRANSIENT (Plex or OpenSubtitles unreachable, quota spent) ->
             caller must NOT stamp; the next pass retries.

    Goes through acquire_subtitle_text, so it gets the sidecar when Plex has
    one and the OpenSubtitles fallback when it does not — most series here
    only have embedded tracks, which no Plex endpoint will hand out.
    """
    got = await acquire_subtitle_text(str(plex_rating_key), **(hints or {}))
    if got is None:
        return None
    if not got or not isinstance(got, tuple):
        return {}
    txt, meta = got
    m = subtitle_metrics(parse_cues(txt), meta.get("duration_min") or 0,
                         track_name=meta.get("track_name") or "")
    if not m:
        return {}
    m["language"] = meta.get("language") or ""
    m["source"] = meta.get("source") or ""
    return m


def _resolve_plex_key(media_type: str, tmdb_id=None, tvdb_id=None):
    """The REAL Plex ratingKey for a candidate, plus its runtime.

    Never read ``item["plex_rating_key"]`` for this: on a deletion candidate
    that field holds the enrichment-pipeline key ("radarr:1234"), not a Plex
    key, so a Plex call built from it 404s every time.
    """
    from src.services.size_norms import tech_profile_for
    prof = tech_profile_for(tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                            media_type=media_type)
    return (prof or {}).get("plex_rating_key"), (prof or {}).get("duration_min")


async def topup_subtitle_metrics(title: str, media_type: str, *,
                                 tmdb_id=None, tvdb_id=None) -> bool:
    """Fetch and persist metrics for one title. True only when numbers landed.

    Idempotent via ``checked`` + ``metrics_v``, the convention every top-up in
    this codebase follows: a version bump re-offers rows stamped under the old
    formula, and a transient failure writes nothing so the next pass retries.
    Never raises — the caller is a warm-up loop that one title must not derail.
    """
    try:
        from src.database.connection import get_db_session
        from src.database.models import MediaSubtitleProfile

        rk, _dur = _resolve_plex_key(media_type, tmdb_id, tvdb_id)
        if not rk:
            return False
        rk = str(rk)

        with get_db_session() as db:
            row = (db.query(MediaSubtitleProfile)
                   .filter(MediaSubtitleProfile.plex_rating_key == rk).first())
            if row and row.checked and row.metrics_v == METRICS_VERSION:
                return False

        m = await fetch_subtitle_metrics(rk, hints={
            "title_hint": title, "media_hint": media_type,
            "tmdb_hint": tmdb_id, "tvdb_hint": tvdb_id})
        if m is None:
            return False                      # transient — do NOT stamp

        with get_db_session() as db:
            row = (db.query(MediaSubtitleProfile)
                   .filter(MediaSubtitleProfile.plex_rating_key == rk).first())
            if not row:
                row = MediaSubtitleProfile(plex_rating_key=rk,
                                           media_type=media_type)
                db.add(row)
            row.title = title
            row.tmdb_id = tmdb_id
            row.tvdb_id = tvdb_id
            row.checked = True
            row.metrics_v = METRICS_VERSION
            if m:
                row.source = m.get("source")
                row.language = m.get("language")
                row.is_sdh = bool(m.get("is_sdh"))
                row.words_per_min = m.get("words_per_min")
                row.silent_min = m.get("silent_min")
                row.silent_share = m.get("silent_share")
                row.mattr = m.get("mattr")
                row.total_words = m.get("total_words")
                row.coverage = m.get("coverage")
            db.commit()
        return bool(m)
    except Exception as e:
        logger.debug("[subtitles] top-up failed for %r: %s", title, e)
        return False


def subtitle_facts(item: dict, media_type: str) -> str:
    """The evidence line for one candidate, or "".

    Pure DB read — no network, no LLM — so it is safe inside the judge funnel,
    which already holds the GPU gate.
    """
    try:
        from src.database.connection import get_db_session
        from src.database.models import MediaSubtitleProfile

        rk, dur = _resolve_plex_key(media_type, item.get("tmdb_id"),
                                    item.get("tvdb_id"))
        if not rk:
            return ""
        with get_db_session() as db:
            row = (db.query(MediaSubtitleProfile)
                   .filter(MediaSubtitleProfile.plex_rating_key == str(rk))
                   .first())
            if not row or not row.words_per_min:
                return ""
            return format_dialogue_line({
                "words_per_min": row.words_per_min,
                "silent_min": row.silent_min or 0.0,
                "duration_min": dur or 0.0,
                "mattr": row.mattr,
                "is_sdh": bool(row.is_sdh),
            }, lang=row.language or "")
    except Exception as e:
        logger.debug("[subtitles] facts lookup failed: %s", e)
        return ""


# ── OPENSUBTITLES FALLBACK ───────────────────────────────────────────────────
# Only ~70% of movies and far less of the series here carry a downloadable
# sidecar track, and embedded tracks are unreachable through the Plex API. This
# closes that gap for anything with an IMDb id — which Plex hands us directly in
# the item's Guid list, so there is no title matching to get wrong.
#
# Quota is the whole design constraint: the anonymous tier is 5 downloads/day
# and a free login 10-20, against up to 60 candidates in a single scan. A VIP
# account raises it to 1000/day, which is what makes batch use viable at all.
# We therefore keep our own conservative day counter and stop BEFORE calling
# out, so a shared account is never drained by us — and an exhausted budget is
# reported as TRANSIENT, never stamped, because it resolves itself tomorrow.

_OS_BASE = "https://api.opensubtitles.com/api/v1"
_OS_UA = "Curatarr/1.0"
_os_token: dict = {"jwt": None, "at": None}


def _os_conf():
    from src.config import settings

    def _plain(v):
        return v.get_secret_value() if hasattr(v, "get_secret_value") else v

    return (_plain(getattr(settings, "OPENSUBTITLES_API_KEY", None)) or "",
            _plain(getattr(settings, "OPENSUBTITLES_USERNAME", None)) or "",
            _plain(getattr(settings, "OPENSUBTITLES_PASSWORD", None)) or "",
            int(getattr(settings, "OPENSUBTITLES_DAILY_BUDGET", 0) or 0))


def _os_budget_key() -> str:
    from datetime import datetime, timezone
    return "subs_os_dl_" + datetime.now(timezone.utc).strftime("%Y%m%d")


def os_spent_today() -> int:
    """Downloads charged to today. Date-scoped, so it resets by itself."""
    try:
        from src.services import app_state
        return int(app_state.get_state(_os_budget_key()) or 0)
    except Exception:
        return 0


def _os_charge() -> None:
    try:
        from src.services import app_state
        app_state.set_state(_os_budget_key(), str(os_spent_today() + 1))
    except Exception as e:
        logger.debug("[subtitles] could not record quota use: %s", e)


async def _os_login(client, api_key: str, user: str, pw: str):
    """Trade credentials for a JWT — only needed to claim an account's larger
    quota. Cached for the day; failure is not fatal, we fall back to the
    anonymous tier."""
    import time
    if _os_token["jwt"] and _os_token["at"] and time.time() - _os_token["at"] < 43200:
        return _os_token["jwt"]
    if not (user and pw):
        return None
    try:
        r = await client.post(_OS_BASE + "/login",
                              headers={"Api-Key": api_key, "User-Agent": _OS_UA,
                                       "Content-Type": "application/json"},
                              json={"username": user, "password": pw})
        r.raise_for_status()
        tok = (r.json() or {}).get("token")
        _os_token["jwt"], _os_token["at"] = tok, time.time()
        return tok
    except Exception as e:
        logger.debug("[subtitles] OpenSubtitles login failed: %s", e)
        return None


async def fetch_opensubtitles(imdb_id: str, languages: str = "en,de",
                              episode_ref=None):
    """Best subtitle text for an IMDb id, or the tri-state signals.

    ``None`` = transient (no key, budget spent, network/5xx) — never stamped.
    ``""``   = the service genuinely has nothing for this title.
    """
    import httpx
    api_key, user, pw, budget = _os_conf()
    if not api_key:
        return None                      # not configured — retry if it ever is
    if budget and os_spent_today() >= budget:
        logger.info("[subtitles] OpenSubtitles daily budget (%d) reached", budget)
        return None                      # transient by design: tomorrow works

    imdb = str(imdb_id or "").lower().replace("tt", "").lstrip("0")
    if not imdb.isdigit():
        return ""
    head = {"Api-Key": api_key, "User-Agent": _OS_UA}
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as c:
            jwt = await _os_login(c, api_key, user, pw)
            if jwt:
                head["Authorization"] = "Bearer " + jwt
            params = {"languages": languages,
                # Let the service exclude what we would otherwise have to strip
                # or refuse: sound-cue tracks and signage-only tracks.
                "hearing_impaired": "exclude", "foreign_parts_only": "exclude",
                "order_by": "download_count", "order_direction": "desc"}
            if episode_ref:
                # A series' IMDb id belongs to the SERIES; the subtitle we want
                # belongs to one episode, so it is looked up as a child.
                params["parent_imdb_id"] = int(imdb)
                params["season_number"] = int(episode_ref[0])
                params["episode_number"] = int(episode_ref[1])
            else:
                params["imdb_id"] = int(imdb)
            r = await c.get(_OS_BASE + "/subtitles", headers=head,
                            params=params)
            if r.status_code == 429:
                return None
            r.raise_for_status()
            data = (r.json() or {}).get("data") or []
            file_id = None
            lang = ""
            for hit in data:
                attrs = hit.get("attributes") or {}
                files = attrs.get("files") or []
                if files and files[0].get("file_id"):
                    file_id = files[0]["file_id"]
                    lang = attrs.get("language") or ""
                    break
            if not file_id:
                return ""                # nothing on file for this title

            d = await c.post(_OS_BASE + "/download", headers={
                **head, "Content-Type": "application/json"},
                json={"file_id": file_id})
            if d.status_code == 429:
                return None
            d.raise_for_status()
            link = (d.json() or {}).get("link")
            if not link:
                return None
            _os_charge()
            txt = (await c.get(link, headers={"User-Agent": _OS_UA})).text
            return (txt, lang)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        return None
    except httpx.HTTPStatusError as e:
        return None if e.response.status_code >= 500 else ""
    except Exception as e:
        logger.debug("[subtitles] OpenSubtitles failed for %s: %s", imdb_id, e)
        return None


async def acquire_subtitle_text(plex_rating_key: str, *, title_hint: str = "",
                                media_hint: str = "", tmdb_hint=None,
                                tvdb_hint=None):
    """Raw subtitle text for a title: sidecar first, OpenSubtitles second.

    Returns ``(text, meta)`` where meta carries language/source/track name,
    or the tri-state ``{}`` / ``None`` as everywhere else in this module.
    The local file always wins — it is free, instant, and matches the exact
    cut on disk, which a downloaded track need not.
    """
    import httpx
    base, token = _plex()
    if not base or not token or not plex_rating_key:
        return {}
    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    imdb_id = ""
    duration_min = 0.0
    episode_ref = None          # (season, episode) when we measured an episode
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(base + "/library/metadata/" + str(plex_rating_key),
                            headers=headers)
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            meta = (r.json().get("MediaContainer", {}).get("Metadata") or [])
            if not meta:
                return {}
            md = meta[0]
            # The series entry carries the IMDb id but NO Media/Part/Stream —
            # subtitles live on the episodes. Read the id here, then descend to
            # one representative episode and measure THAT: its runtime, its
            # track. Using the series' aggregated duration would divide one
            # episode's words by the whole season and report near-silence.
            for g in (md.get("Guid") or []):
                gid = str(g.get("id") or "")
                if gid.startswith("imdb://"):
                    imdb_id = gid.split("//", 1)[1]
                    break
            if md.get("type") == "show" or not md.get("Media"):
                lv = await c.get(base + "/library/metadata/"
                                 + str(plex_rating_key) + "/allLeaves",
                                 headers=headers,
                                 params={"X-Plex-Container-Size": 1})
                leaves = (lv.json().get("MediaContainer", {}).get("Metadata")
                          or []) if lv.status_code == 200 else []
                if not leaves:
                    return {}
                ep = await c.get(base + "/library/metadata/"
                                 + str(leaves[0]["ratingKey"]), headers=headers)
                ep.raise_for_status()
                ep_meta = (ep.json().get("MediaContainer", {}).get("Metadata")
                           or [])
                if not ep_meta:
                    return {}
                md = ep_meta[0]
                episode_ref = (md.get("parentIndex") or 1, md.get("index") or 1)
            duration_min = (md.get("duration") or 0) / 60000.0
            media = (md.get("Media") or [{}])[0]
            part = (media.get("Part") or [{}])[0]
            stream = pick_subtitle_stream(part.get("Stream") or [])
            if stream:
                txt = (await c.get(base + stream["key"],
                                   headers={"X-Plex-Token": token})).text
                name = " ".join(str(stream.get(k) or "") for k in
                                ("title", "displayTitle", "extendedDisplayTitle"))
                return (txt, {"language": stream.get("languageTag")
                              or stream.get("language") or "",
                              "source": "plex_sidecar", "track_name": name,
                              "duration_min": duration_min,
                              "episode_ref": episode_ref})
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        return None
    except httpx.HTTPStatusError as e:
        return None if e.response.status_code >= 500 else {}
    except Exception as e:
        logger.debug("[subtitles] %s: plex read failed: %s", plex_rating_key, e)
        return None

    # An operator's own service comes before the public one: better coverage
    # for the catalogue titles that dominate deletion candidates, no daily
    # quota, and no third party involved. Unconfigured, it answers None in a
    # microsecond and we fall through.
    try:
        from src.services import subtitle_provider as _sp
        if _sp.configured():
            ids = _sp.anime_ids_for(title_hint, tmdb_id=tmdb_hint,
                                    tvdb_id=tvdb_hint) if media_hint == "anime" else {}
            if ids or media_hint == "anime":
                txt = await _sp.fetch_best(
                    anidb_id=ids.get("anidb_id"),
                    anilist_id=ids.get("anilist_id"),
                    tvdb_id=ids.get("tvdb_id") or tvdb_hint,
                    episode=(episode_ref[1] if episode_ref else None))
                if txt:
                    return (txt, {"language": "", "source": "provider",
                                  "track_name": "",
                                  "duration_min": duration_min,
                                  "episode_ref": episode_ref})
                # "" means the service genuinely has nothing; None means it
                # could not answer. Neither is a reason to skip the public
                # fallback, so we simply carry on.
    except Exception as e:
        logger.debug("[subtitles] provider leg failed: %s", e)

    if not imdb_id:
        return {}                        # no id to ask with — definitive
    got = await fetch_opensubtitles(imdb_id, episode_ref=episode_ref)
    if got is None:
        return None
    if not got:
        return {}
    txt, lang = got
    return (txt, {"language": lang, "source": "opensubtitles",
                  "track_name": "", "duration_min": duration_min,
                  "episode_ref": episode_ref})


async def fetch_subtitle_transcript(plex_rating_key: str,
                                    max_chars: int = 12000):
    """Cleaned dialogue text for ONE title — the Level-2 deep read.

    Never used by the batch judge: a feature runs ~13,000 tokens, and the
    curator's whole context is 16k. Even here the text is sampled rather than
    truncated — beginning, middle and end in equal parts — because a blind
    head-cut hides exactly the late shift in register a discussion is usually
    about (the lesson the significance slicer already encodes).
    """
    got = await acquire_subtitle_text(str(plex_rating_key))
    if not got or not isinstance(got, tuple):
        return ""
    txt, meta = got
    cues = parse_cues(txt)
    if not cues or looks_forced(cues, meta.get("duration_min") or 0):
        return ""
    clean = " ".join(
        ln.strip() for ln in strip_non_dialogue(
            "\n".join(c[2] for c in cues)).splitlines() if ln.strip())
    if len(clean) <= max_chars:
        return clean
    third = max_chars // 3
    n = len(clean)
    return (clean[:third]
            + "\n[…]\n" + clean[n // 2 - third // 2: n // 2 + third // 2]
            + "\n[…]\n" + clean[-third:])
