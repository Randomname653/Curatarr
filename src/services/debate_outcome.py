"""Curatarr — debate outcomes: what the judge proposed, and who convinced whom.

``CuratorResolutionLog`` recorded that a debate ended (kept / deleted,
consensus / override) but not what was at stake or who moved. A row had no
link to its proposal — the judge's score, the CUT / STAGNANT verdict and the
gigabytes could only be guessed back by title — and "consensus" covered both
"you talked the curator out of it" and "it talked you into it". The
wrap-up's debate scorecard needs both.

Who moved (``persuader``):

* an override moved nobody — the user decided over a standing objection;
* no debate (the user never wrote in the thread) moved nobody either;
* a title the judge proposed to cut that was KEPT by consensus: the curator
  conceded, so the user convinced it;
* a title DELETED by consensus after a debate: the curator convinced the
  user if the user had argued to keep it first. Only that question needs a
  model, and the summarizer answers it — for rows written today and for the
  history alike (``label_pending``, the custodian's "debate_outcomes" job),
  so old and new labels come from one classifier.

``link_old_rows`` fills the columns for rows written before they existed:
the proposal is found by user, title and outcome next to the row's time.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

THREAD_PREFIX = "deletion_proposal:"
# How far a resolution row may sit from its proposal's resolved_at and still
# be the same decision (a chat keep writes both in one go; old rows drift).
_LINK_WINDOW = timedelta(days=3)
_LABEL_BATCH = 40
_TURN_CHARS = 400
_MAX_TURNS = 16


def thread_of(proposal_id) -> Optional[str]:
    return f"{THREAD_PREFIX}{proposal_id}" if proposal_id else None


def proposal_id_of(thread_id: Optional[str]) -> Optional[int]:
    if thread_id and thread_id.startswith(THREAD_PREFIX):
        tail = thread_id[len(THREAD_PREFIX):]
        return int(tail) if tail.isdigit() else None
    return None


def user_turns(db, user_id: int, thread_id: Optional[str]) -> int:
    """How often the user wrote in the debate's thread."""
    if not thread_id:
        return 0
    from src.database.models import ConversationMessage as M
    return (db.query(M).filter(M.user_id == user_id, M.thread_id == thread_id,
                               M.role == "user").count())


def rule_persuader(outcome: str, resolution_type: str, turns: int,
                   proposed: bool) -> Optional[str]:
    """Who moved, when the facts decide it; None when only reading the
    debate can tell (a delete by consensus after the user spoke)."""
    if resolution_type == "override" or not turns:
        return "none"
    if outcome == "kept":
        return "user" if proposed else "none"
    return None


def outcome_fields(proposal, outcome: str, resolution_type: str, turns: int,
                   thread_id: Optional[str] = None) -> dict:
    """The structured columns of a resolution-log row. ``proposal`` is the
    DeletionProposal (or its copy) the decision settled, or None."""
    fields = {"thread_id": thread_id or (thread_of(proposal.id) if proposal else None),
              "user_turns": turns}
    if proposal is not None:
        conf = getattr(proposal, "confidence", None)
        fields.update(
            proposal_id=proposal.id,
            score=round(conf * 100, 1) if conf is not None else None,
            verdict="STAGNANT" if getattr(proposal, "stagnant", False) else "CUT",
            storage_mb=getattr(proposal, "storage_mb", None))
    who = rule_persuader(outcome, resolution_type, turns, proposal is not None)
    fields["persuader"] = who
    fields["persuader_basis"] = "rule" if who else "pending"
    return fields


def fields_for(db, user_id: int, outcome: str, resolution_type: str,
               proposal=None, thread_id: Optional[str] = None) -> dict:
    """``outcome_fields`` with the thread's user turns counted. A debate
    thread names its proposal, so a chat keep finds it from the thread."""
    if proposal is None and proposal_id_of(thread_id):
        from src.database.models import DeletionProposal
        proposal = db.get(DeletionProposal, proposal_id_of(thread_id))
        if proposal is not None and proposal.user_id != user_id:
            proposal = None
    tid = thread_id if proposal_id_of(thread_id) else (thread_of(proposal.id) if proposal else None)
    return outcome_fields(proposal, outcome, resolution_type, user_turns(db, user_id, tid), tid)


def safe_fields_for(db, user_id: int, outcome: str, resolution_type: str,
                    proposal=None, thread_id: Optional[str] = None) -> dict:
    """``fields_for`` that never costs the log row it decorates: on a fault the
    row is written without the columns, and ``link_old_rows`` fills them later
    (persuader_basis stays NULL)."""
    try:
        return fields_for(db, user_id, outcome, resolution_type, proposal, thread_id)
    except Exception as e:  # noqa: BLE001 - the resolution itself must still be logged
        logger.warning("[debates] outcome columns not filled for a %s row (%s) - "
                       "the custodian's link pass fills them", outcome, e)
        return {}


# ── rows from before the columns ────────────────────────────────────────────

def link_old_rows(limit: int = 2000) -> int:
    """Fill the structured columns of rows that have none yet (persuader_basis
    NULL): the proposal by user, title and outcome nearest the row's time.
    Blocking — call via ``run_write``. Returns the rows filled."""
    from src.database.models import CuratorResolutionLog as L, DeletionProposal as P
    done = 0
    with get_db_session() as db:
        rows = db.query(L).filter(L.persuader_basis.is_(None)).order_by(L.id).limit(limit).all()
        for r in rows:
            status = "rejected" if r.outcome == "kept" else "deleted"
            cands = (db.query(P).filter(P.user_id == r.user_id, P.title == r.title,
                                        P.status == status, P.resolved_at.isnot(None)).all())
            if r.category:
                cands = [p for p in cands if not p.category or p.category == r.category] or cands
            at = r.created_at or datetime.utcnow()
            near = [p for p in cands if abs(p.resolved_at - at) <= _LINK_WINDOW]
            proposal = min(near, key=lambda p: abs(p.resolved_at - at)) if near else None
            for k, v in fields_for(db, r.user_id, r.outcome, r.resolution_type, proposal).items():
                setattr(r, k, v)
            done += 1
        db.commit()
    if done:
        logger.info("[debates] %d resolution row(s) linked to their proposals", done)
    return done


# ── the summarizer reads the debates the facts cannot decide ────────────────

_LABEL_SYSTEM = (
    "You read a debate between a media library's owner (USER) and its curator "
    "(CURATOR) about whether to delete one title. The curator proposed deleting it, "
    "and in the end the title WAS deleted. Answer one question: before agreeing, did "
    "the USER argue to KEEP it — defend it, push back, give a reason it should stay? "
    "Questions, small talk or asking for details are not arguing to keep. "
    "Answer in JSON: {\"user_argued_to_keep\": true|false, \"evidence\": \"<the user's "
    "own words that show it, max 20 words, or empty>\"}.")
_LABEL_SCHEMA = {"type": "object", "properties": {
    "user_argued_to_keep": {"type": "boolean"}, "evidence": {"type": "string"}},
    "required": ["user_argued_to_keep", "evidence"]}


def _pending(limit: int) -> list:
    from src.database.models import ConversationMessage as M, CuratorResolutionLog as L
    with get_db_session() as db:
        rows = (db.query(L.id, L.user_id, L.title, L.thread_id)
                .filter(L.persuader_basis == "pending").order_by(L.id).limit(limit).all())
        out = []
        for rid, uid, title, tid in rows:
            msgs = (db.query(M.role, M.content).filter(M.user_id == uid, M.thread_id == tid)
                    .order_by(M.id).all()) if tid else []
            out.append({"id": rid, "title": title, "turns": [
                (role, (content or "").strip()[:_TURN_CHARS]) for role, content in msgs
                if role in ("user", "assistant")][-_MAX_TURNS:]})
        return out


def _save_label(row_id: int, who: str, basis: str) -> None:
    from src.database.models import CuratorResolutionLog as L
    with get_db_session() as db:
        row = db.get(L, row_id)
        if row is not None:
            row.persuader, row.persuader_basis = who, basis
            db.commit()


async def _ask(title: str, turns: list) -> Optional[bool]:
    """True / False from the summarizer; None when it could not answer."""
    import json
    from src.config import settings
    from src.services.llm_utils import SUMMARIZER_KEEP_ALIVE, ollama_options, strip_think_tags
    from src.services.ollama_hosts import ollama_for
    from src.services.pc_guard import ollama_client
    debate = "\n".join(f"{'USER' if r == 'user' else 'CURATOR'}: {c}" for r, c in turns)
    model = settings.SUMMARIZER_MODEL or settings.BASE_SUMMARIZER_MODEL
    try:
        async with ollama_client(timeout=120) as client:
            r = await client.post(f"{ollama_for('enrich')}/api/chat", json={
                "model": model, "stream": False, "format": _LABEL_SCHEMA,
                # The summariser's residency: Ollama keeps the LAST request's
                # keep_alive, and its 5 min default would cut the bake short.
                "keep_alive": SUMMARIZER_KEEP_ALIVE,
                "messages": [{"role": "system", "content": _LABEL_SYSTEM},
                             {"role": "user", "content": f"TITLE: {title}\n\n{debate}"}],
                **ollama_options(temperature=0.0, num_predict=120)})
        if r.status_code != 200:
            logger.warning("[debates] summarizer HTTP %s on %r", r.status_code, title)
            return None
        got = json.loads(strip_think_tags(r.json().get("message", {}).get("content", "") or ""))
        return bool(got.get("user_argued_to_keep"))
    except Exception as e:  # noqa: BLE001 - one unanswered debate is retried next run
        logger.warning("[debates] summarizer could not read the %r debate: %s", title, e)
        return None


async def label_pending(limit: int = _LABEL_BATCH, task=None) -> dict:
    """Ask the summarizer about the debates the facts cannot decide. A thread
    with nothing left to read (chat wiped) moved nobody."""
    from src.database.runner import run_read, run_write
    from src.services.task_monitor import task_monitor
    rows = await run_read(_pending, limit)
    counts = {"curator": 0, "none": 0, "unanswered": 0}
    for i, row in enumerate(rows):
        if task is not None:
            task_monitor.update(task, processed=i, total=len(rows),
                                message=f"Reading the {row['title']} debate…")
        if not any(r == "user" for r, _ in row["turns"]):
            await run_write(_save_label, row["id"], "none", "rule")
            counts["none"] += 1
            continue
        argued = await _ask(row["title"], row["turns"])
        if argued is None:
            counts["unanswered"] += 1
            continue
        who = "curator" if argued else "none"
        await run_write(_save_label, row["id"], who, "llm")
        counts[who] += 1
    if rows:
        logger.info("[debates] labelled %d debate(s): %s", len(rows), counts)
    return counts


async def run_debate_outcomes(task=None) -> bool:
    """The custodian job: link rows from before the columns, then label the
    debates still pending. True when nothing is left unanswered."""
    from src.database.runner import run_write
    await run_write(link_old_rows)
    counts = await label_pending(task=task)
    return counts["unanswered"] == 0


def sample_labels(limit: int = 30, user_id: Optional[int] = None) -> list:
    """The summarizer's labels with the user's own words, newest first — for
    the owner to check them against the debates. Blocking — call via
    ``run_read``."""
    from src.database.models import ConversationMessage as M, CuratorResolutionLog as L
    with get_db_session() as db:
        q = db.query(L).filter(L.persuader_basis == "llm")
        if user_id is not None:
            q = q.filter(L.user_id == user_id)
        rows = q.order_by(L.id.desc()).limit(limit).all()
        out = []
        for r in rows:
            said = [c for (c,) in db.query(M.content).filter(
                M.user_id == r.user_id, M.thread_id == r.thread_id, M.role == "user")
                .order_by(M.id).limit(4)]
            out.append({"id": r.id, "title": r.title, "outcome": r.outcome,
                        "persuader": r.persuader, "user_said": [(c or "")[:200] for c in said]})
        return out
