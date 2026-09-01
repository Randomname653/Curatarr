"""Clicking a recently-played poster opens a conversation, not a tribunal.

The last-played strip hands the chat a ``watched_title`` discuss context.
Contract pins:

* the anchor is the user's OWN watch-history row, resolved server-side with
  an ownership filter — client-supplied facts are hints, never truth
  (the same trust model proposals use);
* one thread per WORK, not per episode — S2E5 tonight and S2E6 tomorrow
  continue the same conversation;
* the block states the episode FACT (which one, when, finished or not) and
  disclaims episode-level plot knowledge — the same honesty mechanic the
  dialogue line uses;
* no deletion machinery: the discussion UI block and verdict framing stay
  out unless the user raises deletion themselves.
"""

import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.routers.chat import _thread_id_for
from src.schemas.chat import DiscussContext


def _src(rel: str) -> str:
    return (_ROOT / "src" / rel).read_text(encoding="utf-8")


def test_one_thread_per_work_never_per_episode():
    a = _thread_id_for(DiscussContext(type="watched_title", tmdb_id=95479,
                                      season=2, episode=5))
    b = _thread_id_for(DiscussContext(type="watched_title", tmdb_id=95479,
                                      season=2, episode=6))
    assert a == b == "watched:tmdb:95479"


def test_title_fallback_is_normalised_and_stable():
    a = _thread_id_for(DiscussContext(type="watched_title",
                                      series_title="Mushoku Tensei: Jobless Reincarnation"))
    b = _thread_id_for(DiscussContext(type="watched_title",
                                      series_title="mushoku tensei — jobless reincarnation"))
    assert a == b == "watched:mushoku-tensei-jobless-reincarnation"


def test_the_type_alias_is_honoured_everywhere_kind_is_read():
    """The last-played contract sends `type`; legacy paths send `kind` or
    `action`. Both resolution sites must accept all three."""
    assert _thread_id_for(DiscussContext(kind="watched_title", tmdb_id=7)) == \
           _thread_id_for(DiscussContext(type="watched_title", tmdb_id=7))
    chat = _src("routers/chat.py")
    assert chat.count('ctx.kind or getattr(ctx, "type", None) or ctx.action') == 2


def test_the_branch_anchors_on_the_users_own_row():
    chat = _src("routers/chat.py")
    branch = chat[chat.index('# ── Watched-title discussion'):]
    branch = branch[:branch.index('# ── Proactive-message discussion')]
    assert "WatchHistoryEntry.user_id == user_id" in branch
    # facts come from the resolved row, not the client's copies
    assert "row.season" in branch and "row.viewed_at" in branch
    # the honesty rule and the no-tribunal framing ride along
    assert "EPISODE HONESTY" in branch
    assert "not a " in branch and "deletion review" in branch
    # and the deletion-discussion UI knowledge stays OUT of this branch
    assert "DISCUSSION_UI_BLOCK" not in branch


def test_recent_history_exposes_the_row_id_for_the_precise_anchor():
    assert '"id": e.id,' in _src("routers/history.py")
