"""Debate outcomes: a resolution tied to what the judge proposed, and who
convinced whom.

The resolution log recorded that a debate ended (kept / deleted, consensus /
override) but had no link to its proposal — the judge's score, verdict and
the gigabytes at stake could only be guessed back by title — and
"consensus" covered both "you talked the curator out of it" and "it talked
you into it". Every write path now fills the link; who moved comes from a
rule where the facts decide it and from the summarizer where only the
debate can tell, for old rows and new alike.

    python tests/test_debate_outcome.py
"""
import asyncio
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.routers.recommendations as recs  # noqa: E402
import src.services.debate_outcome as do  # noqa: E402
import src.services.episodic_memory as em  # noqa: E402
from src.database.models import (Base, ConversationMessage, CuratorResolutionLog,  # noqa: E402
                                 DeletionProposal)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


@contextmanager
def fake_db_session():
    s = Session()
    try:
        yield s
    finally:
        s.close()


recs.get_db_session = fake_db_session
em.get_db_session = fake_db_session
do.get_db_session = fake_db_session
stance = {"polarity": "CONFIRMED"}
recs._latest_curator_stance_for_proposal = (
    lambda db, uid, pid, fallback_pitch=None: (fallback_pitch or "s", stance["polarity"]))


async def _deleted(p):
    return True


async def _no_guard(p):
    return None


recs._execute_arr_delete = _deleted
recs._lidarr_freshness_guard = _no_guard
_next = [100]


def seed(title, stagnant=False, status="pending", resolved_at=None):
    _next[0] += 1
    with fake_db_session() as s:
        s.add(DeletionProposal(id=_next[0], user_id=1, title=title, service="radarr",
                               media_id=str(_next[0]), reason="pitch", confidence=0.83,
                               storage_mb=40960, status=status, category="movie",
                               stagnant=stagnant, resolved_at=resolved_at))
        s.commit()
    return _next[0]


def say(pid, *turns):
    with fake_db_session() as s:
        for role, text in turns:
            s.add(ConversationMessage(user_id=1, role=role, content=text,
                                      thread_id=do.thread_of(pid)))
        s.commit()


def log_of(title):
    with fake_db_session() as s:
        r = s.query(CuratorResolutionLog).filter_by(title=title).one()
        s.expunge(r)
        return r


class _U:
    id = 1


# ── the rule ─────────────────────────────────────────────────────────────────
check("an override moved nobody", do.rule_persuader("kept", "override", 5, True) == "none")
check("no debate moved nobody", do.rule_persuader("deleted", "consensus", 0, True) == "none")
check("a proposed cut kept by consensus: the user convinced the curator",
      do.rule_persuader("kept", "consensus", 2, True) == "user")
check("a keep with no proposal on the table convinced nobody",
      do.rule_persuader("kept", "consensus", 2, False) == "none")
check("a delete by consensus after a debate needs the debate read",
      do.rule_persuader("deleted", "consensus", 3, True) is None)
check("a thread names its proposal", do.proposal_id_of("deletion_proposal:42") == 42
      and do.proposal_id_of("general") is None)

# ── card Keep ────────────────────────────────────────────────────────────────
pid = seed("Grown Ups 2")
db = Session()
recs.reject_deletion(pid, user=_U(), db=db)
db.close()
r = log_of("Grown Ups 2")
check("a card Keep is tied to its proposal: score, verdict, GB, thread",
      r.proposal_id == pid and r.score == 83.0 and r.verdict == "CUT"
      and r.storage_mb == 40960 and r.thread_id == f"deletion_proposal:{pid}")
check("...and with no debate it moved nobody",
      r.resolution_type == "override" and r.user_turns == 0
      and (r.persuader, r.persuader_basis) == ("none", "rule"))

pid = seed("Twilight", stagnant=True)
say(pid, ("assistant", "It is clutter."), ("user", "My partner rewatches it every winter."),
    ("assistant", "Fair. REVERSED: keep it."))
stance["polarity"] = "REVERSED"
db = Session()
recs.reject_deletion(pid, user=_U(), db=db)
db.close()
stance["polarity"] = "CONFIRMED"
r = log_of("Twilight")
check("a Keep after the curator conceded: the user convinced it",
      r.resolution_type == "consensus" and r.user_turns == 1 and r.verdict == "STAGNANT"
      and (r.persuader, r.persuader_basis) == ("user", "rule"))

# ── delete ───────────────────────────────────────────────────────────────────


def delete(pid):
    p = recs._load_proposal(1, pid)
    asyncio.run(recs._delete_one_and_log(1, p))
    asyncio.run(recs._commit_outcome(p))


pid = seed("Fast X")
delete(pid)
r = log_of("Fast X")
check("a delete without a debate moved nobody, and carries the GB freed",
      r.outcome == "deleted" and r.storage_mb == 40960 and r.proposal_id == pid
      and (r.persuader, r.persuader_basis) == ("none", "rule"))

pid = seed("Transformers: The Last Knight")
say(pid, ("user", "But the IMAX scenes!"), ("assistant", "Four sequels deep."),
    ("user", "Fine, delete it."))
delete(pid)
r = log_of("Transformers: The Last Knight")
check("a delete after a debate waits for the summarizer",
      r.user_turns == 2 and r.persuader is None and r.persuader_basis == "pending")
DEBATED = r.id

pid_quiet = seed("Quiet Delete")
say(pid_quiet, ("assistant", "This one goes."), ("user", "ok"))
delete(pid_quiet)

# ── chat keep ────────────────────────────────────────────────────────────────
pid = seed("Mamma Mia!")
say(pid, ("user", "We are keeping this, it is my sister's favourite."))
asyncio.run(em.handle_protection_intent(
    1, "ACTION: PROTECT_MEDIA | TITLE: Mamma Mia! | REASON: family | RESOLUTION: consensus",
    category="movie", thread_id=f"deletion_proposal:{pid}"))
r = log_of("Mamma Mia!")
check("a chat keep in the proposal's thread is tied to it",
      r.proposal_id == pid and r.user_turns == 1 and r.persuader == "user")

pid = seed("Heat")
asyncio.run(em.handle_protection_intent(
    1, "ACTION: PROTECT_MEDIA | TITLE: Heat | REASON: classic | RESOLUTION: override",
    category="movie", thread_id="general"))
r = log_of("Heat")
check("a chat keep outside the thread finds the proposal it just closed",
      r.proposal_id == pid and r.persuader == "none")

# ── rows from before the columns ─────────────────────────────────────────────
T = datetime(2026, 3, 1, 20)
old_pid = seed("Old Keep", status="rejected", resolved_at=T)
say(old_pid, ("user", "No, this stays."))
with fake_db_session() as s:
    s.add(CuratorResolutionLog(user_id=1, title="Old Keep", category="movie", outcome="kept",
                               resolution_type="consensus", created_at=T + timedelta(minutes=2)))
    s.add(CuratorResolutionLog(user_id=1, title="Orphan Keep", category="movie", outcome="kept",
                               resolution_type="override", created_at=T))
    s.add(CuratorResolutionLog(user_id=1, title="Far Away", category="movie", outcome="kept",
                               resolution_type="consensus", created_at=T + timedelta(days=30)))
    s.commit()
seed("Far Away", status="rejected", resolved_at=T)
n = do.link_old_rows()
r = log_of("Old Keep")
check("an old row finds its proposal by title and time, and gets the rule's answer",
      r.proposal_id == old_pid and r.score == 83.0 and r.persuader == "user")
check("an old row with no proposal is processed once, moving nobody",
      log_of("Orphan Keep").persuader_basis == "rule" and log_of("Orphan Keep").proposal_id is None)
check("a proposal resolved a month away is not the same decision",
      log_of("Far Away").proposal_id is None)
check("rows already filled are left alone", n == 3 and do.link_old_rows() == 0)

# ── the summarizer reads the rest ────────────────────────────────────────────
asked = []
answers = {"Transformers: The Last Knight": True, "Quiet Delete": False}


async def fake_ask(title, turns):
    asked.append((title, turns))
    return answers.get(title)


REAL_ASK = do._ask
do._ask = fake_ask
counts = asyncio.run(do.label_pending())
check("the user argued to keep, then agreed: the curator convinced them",
      log_of("Transformers: The Last Knight").persuader == "curator"
      and log_of("Transformers: The Last Knight").persuader_basis == "llm")
check("no argument to keep: a delete by consensus moved nobody",
      log_of("Quiet Delete").persuader == "none")
check("the summarizer reads both sides of the debate in order",
      [r for r, _ in dict(asked)["Transformers: The Last Knight"]] == ["user", "assistant", "user"])

pid = seed("Wiped")
say(pid, ("user", "hmm"))
delete(pid)
with fake_db_session() as s:
    s.query(ConversationMessage).filter_by(thread_id=do.thread_of(pid)).delete()
    s.commit()
pid = seed("Unanswered")
say(pid, ("user", "Really?"))
delete(pid)
asked.clear()
counts = asyncio.run(do.label_pending())
check("a debate wiped from chat is not sent to the model and moved nobody",
      log_of("Wiped").persuader == "none" and "Wiped" not in [t for t, _ in asked])
check("a debate the summarizer could not answer stays pending for the next run",
      log_of("Unanswered").persuader_basis == "pending" and counts["unanswered"] == 1)
check("the job reports the unanswered one", asyncio.run(do.run_debate_outcomes()) is False)

labels = do.sample_labels(10, user_id=1)
check("the owner's sample shows each llm label with their own words",
      any(x["title"] == "Transformers: The Last Knight" and x["persuader"] == "curator"
          and "But the IMAX scenes!" in x["user_said"] for x in labels)
      and all(x["persuader"] for x in labels))

# ── the summarizer call itself ───────────────────────────────────────────────
import json  # noqa: E402

import src.services.pc_guard as pcg  # noqa: E402

sent = []


class _Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return {"message": {"content": json.dumps(self._body)}}


class _Client:
    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        sent.append(json)
        return _Resp(self.body)


_real_client = pcg.ollama_client
try:
    pcg.ollama_client = lambda timeout=None: _Client({"user_argued_to_keep": True, "evidence": "IMAX"})
    yes = asyncio.run(REAL_ASK("T", [("user", "But the IMAX scenes!")]))
    pcg.ollama_client = lambda timeout=None: _Client({"user_argued_to_keep": False, "evidence": ""})
    no = asyncio.run(REAL_ASK("T", [("user", "ok")]))
finally:
    pcg.ollama_client = _real_client
check("the summarizer is asked one forced-JSON question and its answer is read",
      yes is True and no is False and sent[0]["format"] == do._LABEL_SCHEMA
      and "USER: But the IMAX scenes!" in sent[0]["messages"][1]["content"])

# ── wiring ───────────────────────────────────────────────────────────────────
from src.services.data_custodian import _registry  # noqa: E402

job = next((t for t in _registry() if t.job_id == "debate_outcomes"), None)
check("the custodian runs it on the summarizer", job is not None and job.llm_role == "summarizer"
      and job.needs_llm and job.takes_task)
src = (Path(__file__).resolve().parents[1] / "src/routers/stats.py").read_text(encoding="utf-8")
check("the label sample is owner-only", '"/debate-labels"' in src and "require_admin" in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
