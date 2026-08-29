"""Subtitle-derived execution signals: parsing, stripping, and honest refusal.

    python tests/run_all.py

Every check here exists because a specific way of being wrong was identified
before the code was written:

* raw type-token ratio falls purely with text length, so a long film would
  look "less varied" than a short one by construction — the code must use a
  length-invariant measure, and the test proves it does by comparing both on
  the same text doubled;
* forced tracks carry only signage and would report a talkative film as
  nearly silent — measuring one is worse than measuring nothing;
* SDH tracks carry bracketed sound cues and lyrics that are not dialogue;
* CJK scripts have no whitespace word boundaries, so a words-per-minute
  figure there is an artifact of the writing system;
* a bare number in an evidence block gets promoted to a verdict by the model
  (the file-size lesson), so the caveat must travel with it, always.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.subtitle_signals import (
    count_sdh_markers, format_dialogue_line, looks_forced, mattr, parse_cues,
    script_of, strip_non_dialogue, subtitle_metrics, tokenize,
)

SRT = """1
00:00:01,000 --> 00:00:03,500
Hello there, old friend.

2
00:00:04,000 --> 00:00:06,000
<i>You look well.</i>

3
00:01:30,000 --> 00:01:32,000
It has been a long time.
"""

SDH_SRT = """1
00:00:01,000 --> 00:00:03,000
[door slams]

2
00:00:04,000 --> 00:00:06,000
MARY: I told you not to come.

3
00:00:07,000 --> 00:00:09,000
♪ and the night goes on ♪

4
00:00:10,000 --> 00:00:12,000
(sighs) Then leave.

5
00:00:13,000 --> 00:00:15,000
DANGER — KEEP OUT
"""


def _synthetic_cues(n, step=10.0, text="some words spoken here"):
    return [(i * step, i * step + 2.0, text) for i in range(n)]


def _distinct_words(n):
    """n genuinely distinct word tokens.

    NOT "w0 w1 w2 ...": tokenize() drops digits by design, so numbered words
    all collapse to a single type and any diversity test built on them would
    pass without measuring anything.
    """
    import string
    pairs = [a + b for a in string.ascii_lowercase for b in string.ascii_lowercase]
    assert n <= len(pairs)
    return pairs[:n]


def test_parse_cues_basic():
    cues = parse_cues(SRT)
    assert len(cues) == 3, cues
    assert cues[0][0] == 1.0 and cues[0][1] == 3.5
    assert "old friend" in cues[0][2]
    assert cues[2][0] == 90.0


def test_parse_cues_survives_malformed_blocks():
    broken = SRT + "\n\n99\nNOT A TIMESTAMP\nsome text\n\n100\n00:0X:99,--- --> zz\nmore\n"
    # A bad block must cost only itself, never the other cues.
    assert len(parse_cues(broken)) == 3


def test_parse_cues_accepts_vtt_dot_millis():
    vtt = "WEBVTT\n\n00:00:02.000 --> 00:00:04.000\nA line.\n"
    cues = parse_cues(vtt)
    assert len(cues) == 1 and cues[0][0] == 2.0


def test_parse_cues_empty_and_none():
    assert parse_cues("") == []
    assert parse_cues(None) == []


def test_strip_removes_sound_cues_lyrics_and_signage():
    raw = "\n".join(c[2] for c in parse_cues(SDH_SRT))
    clean = strip_non_dialogue(raw)
    for gone in ("door slams", "sighs", "night goes on", "KEEP OUT", "MARY:"):
        assert gone not in clean, f"{gone!r} survived stripping: {clean!r}"
    # ...while the actual dialogue is untouched.
    assert "I told you not to come." in clean
    assert "Then leave." in clean


def test_strip_keeps_plain_dialogue_intact():
    raw = "\n".join(c[2] for c in parse_cues(SRT))
    clean = strip_non_dialogue(raw)
    assert "Hello there, old friend." in clean
    assert "You look well." in clean          # italics tag removed, words kept


def test_sdh_markers_separate_the_two_track_kinds():
    sdh = count_sdh_markers("\n".join(c[2] for c in parse_cues(SDH_SRT)))
    plain = count_sdh_markers("\n".join(c[2] for c in parse_cues(SRT)))
    assert sdh >= 4 and plain == 0, (sdh, plain)


# -- the reason this module does not use raw TTR ----------------------------
# Vocabulary grows sublinearly while tokens grow linearly, so plain TTR sinks
# as a text lengthens regardless of how varied it is. MATTR averages over a
# fixed window and does not. Doubling a text is the cleanest demonstration:
# the vocabulary is identical, so a length-invariant measure must not move.

def test_mattr_is_length_invariant_where_raw_ttr_is_not():
    vocab = _distinct_words(137)
    base = tokenize(" ".join(vocab[i % 137] for i in range(400)))
    doubled = base + base
    raw_ttr_base = len(set(base)) / len(base)
    raw_ttr_doubled = len(set(doubled)) / len(doubled)
    assert raw_ttr_doubled < raw_ttr_base * 0.75, (
        "premise broken: raw TTR should collapse when the text is doubled")
    m1, m2 = mattr(base), mattr(doubled)
    assert m1 is not None and m2 is not None
    assert abs(m1 - m2) < 0.02, (m1, m2)


def test_mattr_refuses_texts_shorter_than_its_window():
    assert mattr(tokenize("only a handful of words here")) is None
    assert mattr([]) is None


def test_mattr_ranks_varied_above_repetitive():
    varied = tokenize(" ".join(_distinct_words(300)))
    repetitive = tokenize(" ".join(["yes no"] * 150))
    assert mattr(varied) > mattr(repetitive)


def test_forced_track_detection():
    # A signage-only track: few cues, clustered early in a long film.
    assert looks_forced(_synthetic_cues(20), duration_min=100)
    # Many cues but covering only the first minutes of a feature.
    assert looks_forced(_synthetic_cues(200, step=0.5), duration_min=100)
    # A real track: many cues spanning the runtime.
    assert not looks_forced(_synthetic_cues(600, step=10.0), duration_min=100)
    assert looks_forced([], duration_min=100)


def test_script_detection_reads_text_not_tags():
    assert script_of("Hello there, old friend") == "latin"
    assert script_of("こんにちは、元気ですか") == "cjk"
    assert script_of("") == "other"
    assert script_of("12345 !!! ...") == "other"


def test_metrics_refuse_cjk_because_word_counts_are_meaningless():
    cues = _synthetic_cues(600, step=10.0, text="こんにちは、元気ですか")
    assert subtitle_metrics(cues, 100.0) == {}


def test_metrics_refuse_forced_and_empty_input():
    assert subtitle_metrics(_synthetic_cues(20), 100.0) == {}
    assert subtitle_metrics([], 100.0) == {}
    assert subtitle_metrics(_synthetic_cues(600), 0) == {}


def test_metrics_shape_and_silence_arithmetic():
    # 600 cues, one every 10s, each 2s long -> an 8s gap: never "silent".
    talky = subtitle_metrics(_synthetic_cues(600, step=10.0), 100.0)
    assert talky and talky["silent_min"] == 0.0
    # Same cue count spread over 60s steps -> 58s gaps, all silent.
    quiet = subtitle_metrics(_synthetic_cues(600, step=60.0), 600.0)
    assert quiet["silent_min"] > 500
    assert quiet["words_per_min"] < talky["words_per_min"]
    for key in ("words_per_min", "silent_share", "coverage", "total_words",
                "cue_count", "metrics_v"):
        assert key in talky


def test_sdh_flag_from_track_name_and_from_content():
    cues = _synthetic_cues(600, step=10.0)
    assert subtitle_metrics(cues, 100.0, track_name="English (SDH)")["is_sdh"]
    assert not subtitle_metrics(cues, 100.0, track_name="English")["is_sdh"]


# -- the line the judge actually sees ---------------------------------------

def test_line_is_empty_when_there_is_nothing_to_say():
    assert format_dialogue_line({}) == ""
    assert format_dialogue_line(None) == ""


def test_line_always_carries_its_caveat():
    m = subtitle_metrics(_synthetic_cues(600, step=10.0), 100.0)
    line = format_dialogue_line(m, lang="en")
    assert line.startswith("DIALOGUE:")
    assert "words/min" in line and "without dialogue" in line
    # The file-size lesson: the number may never travel without the sentence
    # that stops it from becoming a verdict.
    assert "indicative, never decisive" in line
    assert "condensed by design" in line
    assert "not penalised here" in line


# -- the wiring: where the numbers go, and what the law says about them ------
# These are source-level guards, in the style of tests/test_evidence_guards.py:
# the value of the metric depends entirely on the sentence that travels with
# it, so a refactor must not be able to separate them silently.

_SRC = Path(__file__).resolve().parents[1] / "src"
_PILLARS = (_SRC / "services" / "pillars.py").read_text(encoding="utf-8")
_ENGINE = (_SRC / "services" / "recommendations_engine.py").read_text(encoding="utf-8")
_SIGNALS = (_SRC / "services" / "subtitle_signals.py").read_text(encoding="utf-8")


def test_evidence_block_asks_for_the_dialogue_line():
    assert "from src.services.subtitle_signals import subtitle_facts" in _PILLARS
    assert "dialogue_line" in _PILLARS and '+ dialogue_line' in _PILLARS
    assert '"dialogue_signal": False' in _PILLARS


def test_law_forbids_reading_sparse_dialogue_as_a_defect():
    # One edit to _PILLARS_BODY reaches the judge and the discussion alike,
    # since both interpolate it — so this single check covers both surfaces.
    assert "Sparse dialogue is not thin writing" in _PILLARS
    assert "never a defect" in _PILLARS.lower() or "NEVER a defect" in _PILLARS
    assert "condensed by design" in _PILLARS


def test_dialogue_line_survives_the_monologue_strip():
    # _lean_facts removes OWNER TASTE / TECH / OWNER SIGNAL before the prose
    # pass. DIALOGUE is execution evidence the pitch may legitimately cite —
    # it must NOT join that list.
    strip_block = _PILLARS.split("def _lean_facts")[1][:600]
    assert "DIALOGUE" not in strip_block


def test_metrics_are_fetched_outside_the_gpu_gate():
    # The fetch is network I/O; build_evidence runs while the curator holds
    # the GPU. So the top-up belongs in the pre-judge warm-up, and the judge
    # side must be a pure DB read.
    assert "topup_subtitle_metrics" in _ENGINE
    assert "topup_subtitle_metrics" not in _PILLARS
    # The real condition: the fetch must happen BEFORE the curator takes the
    # GPU gate, next to the other top-ups — not inside the judging loop.
    call_at = _ENGINE.index("topup_subtitle_metrics(")
    gate_at = _ENGINE.index("await curator_start(_gate_label")
    assert call_at < gate_at, "subtitle fetch must precede curator_start"
    sig_at = _ENGINE.index("topup_significance(")
    assert abs(call_at - sig_at) < 3000, "should sit with the other top-ups"


def test_module_never_trusts_the_candidate_plex_key():
    # A deletion candidate carries "radarr:1234" in plex_rating_key — the
    # enrichment key, not a Plex one. Resolving through the tech profile is
    # the only path that yields a key Plex will answer to.
    assert "_resolve_plex_key" in _SIGNALS
    assert "tech_profile_for" in _SIGNALS
    assert 'item.get("plex_rating_key")' not in _SIGNALS


def test_tri_state_contract_is_documented_and_distinguished():
    # Empty dict and None must mean different things, or a network blip gets
    # stamped as "this title has no subtitles" forever.
    assert "TRI-STATE" in _SIGNALS
    assert "do NOT stamp" in _SIGNALS or "must NOT\n    stamp" in _SIGNALS
    assert "if m is None:" in _SIGNALS


# -- the OpenSubtitles fallback: quota is the design constraint --------------
# The anonymous tier allows 5 downloads a day and a free login 10-20, against
# up to 60 candidates in one scan; only a VIP account (1000/day) makes batch
# use viable. So the budget is checked BEFORE any network call, and running
# out must read as TRANSIENT — stamping it would mark a title "has no
# subtitles" forever because we happened to be busy that day.

import asyncio as _asyncio

import src.services.subtitle_signals as _ss


def _run(coro):
    return _asyncio.run(coro)


def test_no_api_key_is_transient_not_a_verdict():
    orig = _ss._os_conf
    _ss._os_conf = lambda: ("", "", "", 400)
    try:
        assert _run(_ss.fetch_opensubtitles("tt0096734")) is None
    finally:
        _ss._os_conf = orig


def test_exhausted_budget_never_touches_the_network():
    calls = []
    orig_conf, orig_spent = _ss._os_conf, _ss.os_spent_today
    _ss._os_conf = lambda: ("key", "", "", 10)
    _ss.os_spent_today = lambda: 10          # budget already used up
    import httpx
    orig_client = httpx.AsyncClient

    class _Tripwire(orig_client):
        def __init__(self, *a, **k):
            calls.append(1)
            super().__init__(*a, **k)

    httpx.AsyncClient = _Tripwire
    try:
        assert _run(_ss.fetch_opensubtitles("tt0096734")) is None
        assert not calls, "budget gate must stop BEFORE opening a connection"
    finally:
        _ss._os_conf, _ss.os_spent_today = orig_conf, orig_spent
        httpx.AsyncClient = orig_client


def test_non_numeric_imdb_id_is_definitive_not_transient():
    orig = _ss._os_conf
    _ss._os_conf = lambda: ("key", "", "", 400)
    try:
        # No usable id to ask with — asking again tomorrow cannot help.
        assert _run(_ss.fetch_opensubtitles("")) == ""
        assert _run(_ss.fetch_opensubtitles("not-an-id")) == ""
    finally:
        _ss._os_conf = orig


def test_budget_counter_is_day_scoped():
    # The key carries the date, so yesterday's spend can never block today.
    from datetime import datetime, timezone
    assert datetime.now(timezone.utc).strftime("%Y%m%d") in _ss._os_budget_key()


# -- the level-2 transcript: sampled, capped, and out of the batch path ------

def test_transcript_samples_rather_than_truncates():
    # A blind head-cut hides a late shift in register, which is often exactly
    # what a discussion turns on.
    src = _SIGNALS.split("async def fetch_subtitle_transcript")[1][:1600]
    assert "max_chars" in src
    assert "[…]" in src or "\\u2026" in src
    assert "n // 2" in src, "middle third must be sampled, not just head+tail"


def test_transcript_reaches_only_the_discussion():
    chat = (_SRC / "routers" / "chat.py").read_text(encoding="utf-8")
    assert "fetch_subtitle_transcript" in chat
    # Never the batch judge: one feature is ~13k tokens against a 16k context.
    assert "fetch_subtitle_transcript" not in _PILLARS
    assert "fetch_subtitle_transcript" not in _ENGINE
    # ...and only behind the explicit "look deeper" gate.
    gate = chat.index('if getattr(ctx, "reevaluate", False):')
    assert chat.index("fetch_subtitle_transcript") > gate


def test_transcript_excerpt_declares_what_it_is():
    chat = (_SRC / "routers" / "chat.py").read_text(encoding="utf-8")
    seg = " ".join(chat.split("DIALOGUE EXCERPT")[1][:600].split())
    assert "condensed by design" in seg
    assert "transcript of record" in seg


def test_sidecar_is_preferred_over_the_paid_fallback():
    # The local file is free, instant, and matches the cut on disk — the
    # download is only for what Plex cannot give us.
    src = _SIGNALS.split("async def acquire_subtitle_text")[1]
    assert "pick_subtitle_stream" in src and "fetch_opensubtitles" in src
    assert src.index("pick_subtitle_stream") < src.index("fetch_opensubtitles")


# -- ASS/SSA: the format anime actually ships in -----------------------------
# Not "SRT with different punctuation": field ORDER is declared per file,
# timestamps are centiseconds, and one file carries parallel tracks — the
# dialogue plus translated signs plus opening karaoke. Measuring the signs
# track reports a talkative episode as nearly silent, the same damage a
# forced track does.

ASS = r"""[Script Info]
Title: Fansub

[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,Arial,20
Style: Signs,Arial,20

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:38.62,0:00:40.24,Default,,0,0,0,,Hello there, old friend.
Dialogue: 0,0:00:41.00,0:00:43.50,Default,Ken,0,0,0,,{\i1}You look well.{\i0}
Dialogue: 0,0:00:44.00,0:00:46.00,Default,,0,0,0,,Line one\NLine two
Comment: 0,0:00:47.00,0:00:48.00,Default,,0,0,0,,translator note, ignore me
Dialogue: 0,0:00:50.00,0:00:52.00,Signs,,0,0,0,,{\pos(120,50)}TOKYO STATION
Dialogue: 0,0:00:53.00,0:00:55.00,OP-Karaoke,,0,0,0,,{\k30}na{\k22}mi
Dialogue: 0,0:00:56.00,0:00:58.00,Default,,0,0,0,,{\p1}m 0 0 l 100 0 100 100{\p0}
"""


def test_ass_is_recognised_and_timed_in_centiseconds():
    cues = parse_cues(ASS)
    assert cues, "ASS must not fall through to the SRT path"
    # 0:00:38.62 is 38.62 seconds — reading .62 as milliseconds would give
    # 38.00062 and every duration would collapse.
    assert abs(cues[0][0] - 38.62) < 0.01, cues[0]
    assert abs(cues[0][1] - 40.24) < 0.01


def test_ass_skips_comments_signs_and_karaoke():
    texts = " ".join(c[2] for c in parse_cues(ASS))
    assert "ignore me" not in texts, "Comment: events are not rendered"
    assert "TOKYO" not in texts, "a signs style is not dialogue"
    assert "{\\k30}" not in texts, "karaoke styles are not dialogue"
    assert "old friend" in texts and "You look well" in texts


def test_ass_line_breaks_and_drawings():
    cues = parse_cues(ASS)
    joined = " ".join(c[2] for c in cues)
    assert "Line one Line two" in joined, "\\N is a line break, not a word"
    # A cue that was only a vector drawing must not count as speech.
    assert all(strip_non_dialogue(c[2]).strip() for c in cues)


def test_ass_reads_its_own_field_order():
    # Groups do reorder the Format line; a hardcoded index would read the
    # style name as a timestamp and silently drop everything.
    reordered = ("[Events]\n"
                 "Format: Start, End, Style, Text\n"
                 "Dialogue: 0:01:00.00,0:01:02.00,Default,Reordered works.\n")
    cues = parse_cues(reordered)
    assert len(cues) == 1 and abs(cues[0][0] - 60.0) < 0.01
    assert "Reordered works." in cues[0][2]


def test_ass_commas_inside_dialogue_survive():
    # The text field is the last one, so the split must be bounded — an
    # unbounded split would truncate at the first comma in the line.
    one = ("[Events]\n"
           "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
           "MarginV, Effect, Text\n"
           "Dialogue: 0,0:00:10.00,0:00:12.00,Default,,0,0,0,,"
           "Wait, stop, listen to me.\n")
    cues = parse_cues(one)
    assert len(cues) == 1
    assert cues[0][2] == "Wait, stop, listen to me.", cues[0][2]


def test_ass_metrics_run_end_to_end():
    # Same pipeline the judge uses, on an ASS source.
    body = ["[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text"]
    for i in range(400):
        t = i * 6
        body.append(f"Dialogue: 0,0:{t // 60:02d}:{t % 60:02d}.00,"
                    f"0:{(t + 2) // 60:02d}:{(t + 2) % 60:02d}.00,"
                    f"Default,,0,0,0,,some spoken words here now")
    m = subtitle_metrics(parse_cues("\n".join(body)), 40.0)
    assert m and m["words_per_min"] > 0 and m["cue_count"] == 400
