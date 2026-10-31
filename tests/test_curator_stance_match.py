"""Which deletion candidates the curator took a position on in chat
(2026-10-25, recommendations_engine.positioned_titles).

A plain lower-case substring test exempted every short title from the
deletion run: "Her" sits in every "other", "IF" in every "if", "10" in
any number. On the owner's real 60 days of chat that was 458 titles never
proposed; with whole titles, emphasised when they are one word, 116.

    python tests/test_curator_stance_match.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.recommendations_engine import positioned_titles  # noqa: E402

from _checks import check  # noqa: E402


TEXT = ("The Big Bang Theory is a long-running sitcom; if you ask me, the other shows in 10 years "
        "will not matter. **Her** is a different case, and \"Us\" too. Back to the Future Part III "
        "is the weakest of the three. Sam has binged it.")
TITLES = ["The Big Bang Theory", "Big", "IF", "Her", "Us", "10", "The Other", "Back to the Future Part II",
          "Back to the Future Part III", "Sam", "Bang"]
got = positioned_titles(TEXT, TITLES)
check("a title of several words, whole and in its own capitalisation",
      "The Big Bang Theory" in got and "Back to the Future Part III" in got)
check("...not as the start of a longer one", "Back to the Future Part II" not in got)
check("a one-word title only when emphasised or quoted", "Her" in got and "Us" in got)
check("prose words and numbers are no titles",
      not {"Big", "IF", "10", "Sam", "Bang"} & got)
check("the case of the title counts for a phrase", "The Other" not in got)
check("nothing in, nothing out", positioned_titles("", TITLES) == set() and positioned_titles(TEXT, []) == set())

src = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("the deletion run uses it", "curator_positioned = positioned_titles(_blob, candidate_titles)" in src
      and "if t and t.lower() in _blob:" not in src)

check.done()
