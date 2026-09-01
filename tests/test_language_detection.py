"""The curator answers in the language it is spoken to — and only that.

Provoked by a live failure in a deletion discussion: an English sentence
ending in the typo "aöö" (l and ö are neighbours on a German keyboard, the
user meant "at all") carried two umlauts, and the detector's unconditional
2-umlaut rule flipped the reply to German. The user's short English
follow-up ("what did you say?") fell under the 20-char floor, the thread
fallback re-read the poisoned message, and the curator answered in German
AGAIN — then, asked why, invented a motive out of the taste profile
("your preference for K.I.Z and Alligatoah"). One typo, three failures:
a false language flip, a poisoned thread fallback, and a confabulated
explanation.

The detector fix is symmetric evidence: umlauts only decide when the text
does not read as English around them. The confabulation gets a prompt
guard: the directive now says what the default is based on, so the model
has a true answer available when asked.
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.services.llm_utils import detect_user_language, language_directive


def _lang(msg):
    """Classify a single live message (rule 1 — no db needed)."""
    return detect_user_language(user_id=1, db=None, thread_id=None,
                                current_message=msg)


class _Msg:
    def __init__(self, content):
        self.content = content


class _Q:
    """Minimal stand-in for the SQLAlchemy query chain the fallback uses."""
    def __init__(self, msgs):
        self._msgs = msgs

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def all(self):
        return self._msgs


class _Db:
    def __init__(self, msgs):
        self._msgs = [_Msg(m) for m in msgs]

    def query(self, *cols):
        return _Q(self._msgs)


AOO = ("hah 11gb of bloat while talking about gigantic breasts is kinda "
       "funny but the movie is abolute trash you are completely right on "
       "that one its so stupid but this time it does hurt and having big "
       "breasted women running arround does not safe this at aöö")


def test_a_keyboard_typo_does_not_flip_an_english_message_to_german():
    """The live case verbatim: two umlauts from one slip of the finger,
    surrounded by unmistakably English text."""
    assert _lang(AOO) == "en"


def test_metal_umlauts_do_not_flip_english_either():
    """Band names carry umlauts on purpose — still not German."""
    assert _lang("Motörhead and Blue Öyster Cult are great bands "
                 "from the seventies") == "en"


def test_genuine_german_with_umlauts_is_still_detected():
    # umlauts + a German function word
    assert _lang("ich möchte, dass du das nicht löschst") == "de"
    # umlauts with NO function words — but no English ones either
    assert _lang("gelöschte grüße, völlig überflüssig") == "de"


def test_umlautless_german_is_detected_by_token_density():
    assert _lang("ich denke das ist nicht der fall aber wenn wir "
                 "jetzt mal ehrlich sind") == "de"


def test_plain_english_is_english():
    assert _lang("please delete this movie, it is genuinely terrible") == "en"


def test_a_short_message_with_no_thread_defaults_to_english():
    assert _lang("what did you say?") == "en"


def test_the_thread_fallback_is_not_poisoned_by_the_typo():
    """Second failure in the live case: 'what did you say?' is under the
    20-char floor, so the thread's own messages decide — and they held the
    aöö message. The concatenated blob must still read as English."""
    db = _Db([AOO,
              "but its nice to look at",
              "but they have gigantic titties"])
    assert detect_user_language(user_id=1, db=db, thread_id="t1",
                                current_message="what did you say?") == "en"


def test_the_thread_fallback_still_finds_genuine_german():
    db = _Db(["ich finde den film eigentlich ganz gut",
              "warum schlägst du das überhaupt vor",
              "das ist doch nicht dein ernst"])
    assert detect_user_language(user_id=1, db=db, thread_id="t1",
                                current_message="ok?") == "de"


def test_the_directive_gives_the_model_a_true_answer_about_switches():
    """Third failure: asked why it switched, the model had no true answer
    available and invented one from the taste profile. The directive now
    states what the default mirrors and forbids invented motives."""
    for code in ("de", "en"):
        d = language_directive(code)
        assert "mirrors the user's own recent messages" in d
        assert "never invent a motive" in d
        assert "never argue" in d
