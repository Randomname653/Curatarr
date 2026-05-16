# Curatarr Refactoring CHANGELOG

Branch: `pass-1.5/critical-fixes`
Baseline: `origin/main` at `4fa89c8` ("feat: curator messages — one at a time, skip, English-only, adult content")
Span: 13 commits, ~7800 net insertions / ~1700 deletions across 50+ files.

The work started as a "Pass 1.5" sweep of the audit findings and grew, by
agreement with the project owner, into a multi-pass refactor that

* brought ~3,800 lines of previously-uncommitted live source into git,
* fixed the structural bugs surfaced by the original audit + the
  audits triggered by each subsequent change,
* introduced multi-user attribution as a real architectural feature
  rather than an in-line check, and
* finally surfaced the relevant administrative levers in a Settings
  sidebar instead of buried backend endpoints.

This document is the running summary of what landed, why, and what is
still open. It's intended to be the canonical reference when revisiting
the branch — every "Pass N" section maps to one or more commits.

---

## Completed work

| # | Commit | Pass | Theme | LOC |
|---|---|---|---|---|
| 1 | `71d0f55` | Restore | Bring uncommitted live state into version control | +5934 / −960 |
| 2 | `9a54ab9` | 1.5 | Critical-path fixes + discuss_context RAG refactor | +514 / −646 |
| 3 | `4cbd353` | 1.5-fu | UTC consistency in heuristics text | +2 / −2 |
| 4 | `4b00e2b` | 3.5 | Thread isolation + discuss-flow memory hygiene | +164 / −32 |
| 5 | `53649f9` | 4 | Per-Plex-account attribution + admin re-attribute | +422 / −12 |
| 6 | `845de48` | 4-ui | Wire admin maintenance buttons in History view | +57 |
| 7 | `1f0c330` | 5 | Settings sidebar with Account (PIN) + Maintenance | +358 / −27 |
| 8 | `c3661a8` | 4-fu | Paginate /status/sessions/history/all | +81 / −54 |
| 9 | `32aa765` | 4-fu | Surface Plex-history coverage diagnostics | +49 / −3 |
| 10 | `6e368a4` | 4-fu | Exclude all source=spotify rows in re-attr/cleanup | +20 |
| 11 | `eaec36d` | 4-fu | Reframe re-attribute result as expected steady state | +30 / −15 |
| 12 | `b0a7085` | 6a | Polling leaks + sendFeedback wired + source mismatch | +120 / −21 |
| 13 | `0b36d46` | 6b | Notifications pane with per-trigger toggles | +219 / −5 |

---

### Pass 0 — Restore (`71d0f55`)

**Problem:** Months of work lived only in the working directory. `git status` reported "clean main" because the entire `vector_store/` source module was matched by the overly-broad `vector_store/` `.gitignore` pattern, and seven other production modules had simply never been `git add`-ed.

**Resolution:**
- Brought 7 untracked production modules into git: `routers/music.py`, `routers/process_monitor.py`, `services/llm_priority.py`, `services/llm_utils.py`, `services/music_matcher.py`, `services/process_monitor.py`, `services/spotify_client.py`.
- Brought the previously hidden `src/vector_store/{__init__,chromadb_wrapper}.py` into git.
- Brought 4 utility scripts in: `benchmark.py`, `import_spotify.py`, `reset_test_data.py`, `run_pipeline_spotify.py`.
- Updated 21 modified production files that had drifted from origin/main.
- Removed 5 ChromaDB blob files from the index (they should never have been tracked).
- Tightened `.gitignore`:
  - Replaced the over-broad `vector_store/` rule with `data/chromadb/`, `data/cache/`, `chroma_db/`, etc.
  - Added explicit ignores for `spotify_data/` (~485 MB personal data), `.claude/`, `benchmark_results_*.txt`.

**Intentionally left untracked:** `add_col.py` (one-off migration that fits the existing `migrate_*.py` ignore pattern).

---

### Pass 1.5 — Critical-path fixes + discuss_context RAG refactor (`9a54ab9`, `4cbd353`)

A consolidated pass targeting every audit finding that was either a
crash, a security hole, or a clear bug — plus the `discuss_context`
architectural choice (option **(e)** RAG-style context document, agreed
during planning).

**Crashes fixed**
- `chat.py:84` — `clean_llm_text` was called but never imported. Every chat message that hit the entity-detection path was raising `NameError`, swallowed by `except Exception`. Live-chat entity extraction was completely non-functional in production.
- `enrichment.py:357-364` — `compute_taste` had `background_tasks: BackgroundTasks = None` as the default. FastAPI doesn't inject `BackgroundTasks` when a default is set, so the call chain hit `add_task` on `None` → `AttributeError`. Frontend `computeTaste()` 500'd silently.
- `frontend/index.html:89-103` — stray `fetch()` snippets had been pasted into the `<style>` block. Browser-tolerated but unpredictable layout fallout.

**Security**
- `main.py` SPA catch-all now resolves the candidate path and verifies it stays under `_FRONTEND_ROOT.resolve()`. `../`-traversal is rejected.
- `setup.py` endpoints (`/test`, `/complete`, `/build-models`) gated behind a new `require_admin_or_first_run` dependency: open during onboarding (no admin yet), admin-only thereafter. Closes SSRF + .env-overwrite by anonymous callers post-setup.
- `main.py` mounts `enrichment` and `process_monitor` routers with a prefix-level `Depends(require_admin)`. Any logged-in user could previously trigger heavy LLM jobs or VRAM unloads.
- `users.delete_user` blocks self-delete, blocks admin-delete, soft-disables (`is_active=False`) instead of `DROP`. Stops orphaning FK-referencing rows in `watch_history`/`taste_vectors`/memories.
- `tasks.cancel_task` now requires admin. Tasks are server-wide.
- `auth.poll_plex_pin` — replaced the broken `@rate_limit_poll` decorator (kwargs lookup never matched FastAPI's positional path-arg passing) with a real in-process sliding-window check (60 s, 5 polls/PIN).
- `auth.get_current_user` — non-numeric `sub` returns 401 instead of crashing with 500.
- `auth.logout` docstring made honest about JWT statelessness.

**discuss_context refactor (option (e) — RAG-style, no fake assistant turns)**
- New schema fields: `kind` (`"deletion_proposal"` | `"proactive_message"`), `proposal_id`, `message_id`. Legacy fields (`title`, `pitch`, `action`) remain optional for short-term frontend back-compat.
- New `_build_discuss_context_block(ctx, user_id, db)` helper: looks up the actual `DeletionProposal` / `ProactiveMessage` from the DB by id with ownership check, formats it as a `[CURRENT DISCUSSION CONTEXT]` block in the system prompt — same pattern as `episodic_memory.format_memories_for_context`.
- Removed the fake-assistant-`_save_message` calls. The German hardcoded string `"Ich habe '{title}' zur Löschung vorgeschlagen…"` is gone. Untrusted client text is no longer persisted as fabricated assistant output.
- Frontend `respondToMessage` and `discussDeletion` send the new payload shape.

**Cache TTL**
- Recommendations cache + DeletionProposal both consult a new `recs_invalidate_at` app-state key.
- Plex sync sets it when `synced > 0`; enrichment sets it when `processed > 0`.
- Stale recs are now regenerated on next read; old proposals get a `stale: true` flag for the UI.

**Data / logic fixes**
- `plex_sync.py` `viewed_at` now uses `datetime.utcfromtimestamp` (no more reinterpret-as-local).
- `plex_sync.py` in-progress dedup-update filter now includes `viewed_at`. Previously every in-progress update clobbered every prior play of the same item.
- `plex_sync.py` ARR-sync marker (`last_arr_sync_at`) only advances when both Radarr and Sonarr returned cleanly.
- `orphan_repair.py` `viewed_at` uses `utcfromtimestamp`. Backfilled history rows honor Plex's `viewOffset` instead of forging `completed=True` for everything. The personal anime-title hardcode (`{"One Piece", "Berserk", …}`) is gone.
- `heuristics.py` affinity factor sign corrected (high user rating now actually protects). `tmdb_rating` is read from a real `MediaMetadata.tmdb_rating` field instead of a key that never lived in `metadata_ids`. `apply_immunity_protocol` + `protected_tags` are removed (dead pattern, replaced by `ProtectedMedia` table).
- `anime_mapping.py` real `asyncio.Lock` (lazy, bound to the running loop) replaces the bool `_loading` flag. Concurrent callers double-check after acquiring the lock — no more empty-fallback after timeout.
- `llm_utils.clean_llm_text` — `lstrip("json")` (a character-class bug that ate any leading j/s/o/n) replaced with a `removeprefix`-style `_strip_code_fence_lang` helper. Also `strip_think_tags` now strips unclosed `<think>` blocks (truncated outputs no longer leak reasoning).
- `media_enricher.py` main-path ChromaDB write wraps `add_documents` in try/except + delete-and-readd fallback. Re-enrichment of an already-stored item used to crash with duplicate-id; stale embeddings now get refreshed.
- `heuristics._generate_reason` — last two `datetime.now()` calls switched to `datetime.utcnow()` for consistency with the rest of the module (follow-up commit `4cbd353`).

**Dead code removed**
- Models: `MediaItem`, `BatchJob`, unused `JSON` Column type import.
- `connection.py`: unused `asyncio` import, `_db_write_lock` global, no-op `init_db` block.
- Demo `__main__` blocks in: `encryptor`, `metadata_cache`, `embedding_generator`, `heuristics`, `arr_client`.
- `plex_sync._compute_taste_vector` (taste_engine is canonical) and `_post_sync_verification` (no callers).
- `setup_wizard.{CURATOR,SUMMARIZER}_MODELFILE_TEMPLATE` constants.
- `task_monitor.unsubscribe`'s broken `self._subscribers.discard` line.
- `Counter` import in `plex_sync.py` after the dead path was removed.
- `taste_vectors.py` (entire file, ~226 lines, audit-confirmed orphan).

---

### Pass 3.5 — Thread isolation + discuss-flow memory hygiene (`4b00e2b`)

**Problem:** When the user opened a discussion via the Discuss button, the LLM was pulling history and memories from completely unrelated past conversations into the new topic. Worst-case repro: ask the curator to discuss deletion proposal X, get a response about an entirely different title the user had once asked about, with the model citing "as I mentioned earlier" — the model was right that it said it earlier, just in a different conversation.

Three layers were leaking:
1. `_load_conversation` pulled the last 20 `ConversationMessage` rows for the user with no topic filter.
2. `retrieve_memories` used the user's free-form reply as the embedding query; for a generic "lets actually discuss this" the top-k pulled in semantically similar but topic-irrelevant memories.
3. The system prompt put `[CURRENT DISCUSSION CONTEXT]` near the top — by the time the LLM reached the user message it had already committed attention to whatever was in `[MEMORIES]` / `RELEVANT LIBRARY ITEMS`.

**Schema (additive, self-healing migration)**
- `ConversationMessage.thread_id: Optional[String(64)]`. Migration via `_migrate_columns`: legacy rows stay `NULL` and are treated as the `general` thread.

**Logic**
- New `_thread_id_for(ctx)` helper: derives a stable thread id from the discuss context. `general` for free chat, `deletion_proposal:{id}` or `proactive_message:{id}` for an active discussion.
- `_save_message` + `_load_conversation` both take `thread_id`. Load filters strictly per thread (`general` also picks up legacy NULL rows).
- `GET /api/chat/history` accepts `thread_id` query param. `DELETE` accepts optional `thread_id` — without it, full wipe; with it, single-thread reset (so clearing one discussion doesn't take out the rest).

**Memory hygiene during discussions**
- Embedding query becomes `f"{active_title}: {user_message}"` so retrieval is anchored on the topic, not the user's free-form text.
- `_filter_memories_for_topic` strips memories whose `metadata.title` is set to a different title (case-insensitive substring compare). Generic taste observations (no `metadata.title`) survive.
- RAG context query also uses the title-anchored `retrieval_query`.

**Prompt order**
- `[CURRENT DISCUSSION CONTEXT]` now sits LAST, right before the behavior rules — closer to the user message in the attention window.
- New behavior rule (`#3`, only injected when `active_title` is set): explicit topic-lock — "Other titles in [MEMORIES] / RELEVANT LIBRARY ITEMS are background only — DO NOT switch the topic to them."

---

### Pass 4 — Per-Plex-account attribution (`53649f9`, `845de48`, `c3661a8`, `32aa765`, `6e368a4`, `eaec36d`)

The first sub-step of the multi-user roadmap. The `/library/sections/all` endpoint we sync against is account-blind: `lastViewedAt` is a global maximum and `viewCount` is a global count, so every play landed on the admin user regardless of who actually watched it. Cross-referencing against `/status/sessions/history/all` gives us the per-event `accountID`; that's the missing piece to do correct multi-user attribution without a full architectural rewrite.

**Pass 4a — write-side fix for new syncs (`53649f9`)**
- After fetching watched library items, fire `/status/sessions/history/all` and build `(rk, viewedAt) → accountID` lookups (exact + ±90 s fuzzy fallback).
- In the main sync loop, `_resolve_account_for_event()` picks the `accountID` for each `(rating_key, viewed_at)` pair.
- `accountID → User` resolves through the existing `resolved_account_map`: `accountID=1` = admin; numeric IDs match Plex usernames against the local DB.
- When no account resolves (in-progress play not in history yet, or a Plex account whose user hasn't logged into Curatarr), we fall back to admin and increment a new `unattributed` counter. `WatchHistoryEntry.plex_user_id` is set to the actual `accountID` string anyway so a later re-attribution pass can find it.
- `last_sync_ts` was using `datetime.timestamp()` on a naive UTC datetime (reinterpret-as-local bug). Switched to `calendar.timegm()` for deterministic conversion.

**Pass 4b — admin maintenance endpoints (`53649f9`)**
- `reattribute_watch_history()` re-pulls Plex history and fixes `user_id` / `plex_user_id` on every existing `WatchHistoryEntry` whose `plex_item_id` is a numeric Plex ratingKey. Idempotent, safe to run repeatedly. Spotify rows skipped. Bumps `recs_invalidate_at` afterwards so the cache regenerates against the corrected attribution.
- `cleanup_orphan_watch_history()` enumerates every configured library section, collects live ratingKeys, then deletes WatchHistory rows whose `plex_item_id` is a numeric ratingKey no longer present. Spotify entries preserved. Aborts on any section fetch failure so a transient network blip can't accidentally wipe history.
- Both wired through new admin-only routes:
  - `POST /api/history/admin/re-attribute`
  - `POST /api/history/admin/cleanup-orphans`

**Pass 4-ui — UI affordance (`845de48`)**
- Added inline admin-action row in the History view with both buttons, gated on the existing `.admin-action-row` show-on-admin hook. Cleanup gets a `confirm()` dialog, both report results inline.
- (Later moved into Settings → Maintenance in Pass 5.)

**Pass 4 follow-ups (`c3661a8`, `32aa765`, `6e368a4`, `eaec36d`)** — discovered while testing on real data:

1. **Pagination (`c3661a8`)**: First re-attribute reported 160861/161001 unattributable. Plex's `/status/sessions/history/all` returns a default-size container (~100 events) when no `X-Plex-Container-*` headers are sent. Without pagination the cross-reference saw <0.1 % of events. The helper now walks the full history with `X-Plex-Container-Start` / `X-Plex-Container-Size: 1000` and a 5,000,000-event safety cap. The inline history fetch in `sync_plex_history` was also rewritten to call this same helper (removing duplicated code with the same bug).

2. **Diagnostics (`32aa765`)**: After pagination the same numbers repeated, which made it impossible to tell whether the new code had run. The action's response now includes `plex_history_events`, `plex_history_rating_keys`, `local_rks_in_plex_history`, `local_rks_missing_from_plex_history`, and a sample of unattributable rows annotated with `rk_in_plex_history`. The Settings UI prints this as a multi-line summary.

3. **Source filter (`6e368a4`)**: Diagnostics revealed the actual cause. Of 161 k "non-spotify" rows, ~148 k were Spotify plays where `music_matcher` had linked them to a Plex music ratingKey — their `plex_item_id` is now numeric so `~plex_item_id.like("spotify:%")` let them through, but `source='spotify'` correctly tags them as plays that didn't happen on Plex. Both `reattribute_watch_history` and `cleanup_orphan_watch_history` now also filter `OR(source IS NULL, source != 'spotify')`. After this, examined drops from 161 k to ~13 k (37 movies + 241 episodes + 745 anime + 12 042 Plex-music plays), which lines up with Plex's ~11 k retained history events.

4. **UI rewording (`eaec36d`)**: Even after the source-filter fix, only ~140 rows match because Plex itself only retains ~11 k of the user's older `viewed_at` values — older plays are gone from `/status/sessions/history/all`. This is **expected steady-state**, not a failure mode. The action's status field used to render the count in the same green-success line as the other buckets. Reworded into bullet-per-bucket explanatory copy that explicitly calls bucket 3 "expected and harmless", plus a card-description preamble that explains Plex's retention behaviour up front so the result isn't surprising.

---

### Pass 5 — Settings sidebar (`1f0c330`)

The PIN flow had a working backend but no frontend, the multi-user maintenance buttons sat in the History view, and there was no central spot for per-user account settings. Pass 5 adds a proper Settings sidebar item with sub-nav so all of that lives in one place.

**Backend**
- New `UserPinStatus` schema and `GET /api/users/me/pin-status` endpoint — returns `{has_pin, set_at}` so the UI can render either a Set or a Change form. Hash never exposed.
- `POST /api/users/me/pin` extended:
  - First-time set: client sends only `pin`.
  - Change: client must send `current_pin` (PBKDF2-verified against stored hash) and `pin` (the new one). Wrong current PIN → 403. Same-as-current → 400. Salt rotates on every change.
- Schema caps PIN length at 128, rejects `current_pin` on no-existing-PIN as a soft no-op.
- **Encryption-at-rest activation explicitly out of scope** — PIN is registered, hash stored, encryption activates in a follow-up pass that decides server-side vs client-side architecture. UI copy says this clearly.

**Frontend**
- New `Settings` sidebar item visible to every user.
- `#settings-view` with a left sub-nav (`.settings-tab`) and right panes (`.settings-pane`):
  - **Account** (everyone): Plex info, PIN set/change form, sign-out.
  - **Maintenance** (admin-only): Sync, Force sync, Recompute taste, Re-attribute, Cleanup orphans — each in its own labeled card explaining what it does. Cleanup keeps its `confirm()` guard.
  - **Users** (admin-only): mirror of the existing admin-view user table, sharing the renderer via the new `.user-list-target` class so the two surfaces don't drift.
- `openSettingsPane(name, btn)` switches active tab + pane and runs per-pane lazy loaders (`loadPinStatus` for Account, `loadUsers` for Users).
- `loadPinStatus` toggles between Set and Change forms based on the backend response and shows the last-updated timestamp.
- `submitPinSet` / `submitPinChange` validate length, confirm match, current ≠ new before hitting the endpoint, then refresh the status card.
- Maintenance row removed from the History view — it lives in Settings → Maintenance now. History is read-only stats again.
- Settings tabs gated by admin status piggy-back on the existing `.admin-action-row` show-on-admin hook in `setUser`.

---

### Pass 6a — Frontend polish (`b0a7085`)

Polling leaks + the dead `sendFeedback` button + the source-mismatch UX bug, all collected into one front-and-back commit.

**Polling leaks**
- `loadRecs`: one global `_recsPollTimer` + `_recsPollKillswitch` tracked through `_stopRecsPoll()`. Every entry kills the previous poll, category is snapshotted so a stale tab can't render over the new one, and the inner poll bails as soon as `currentRecsCategory` drifts. Previously every category change leaked another `setInterval` that kept fetching forever.
- `startPlexLogin`: PIN polling now has a 15-minute deadline (matches Plex's PIN expiry) so leaving the pin modal open doesn't poll indefinitely. Added `cancelPlexLogin()` to explicitly tear down the timer when the user dismisses the modal.
- `startOnboardingSync`: poll lifted to a module-level `_onboardingPoll` with `_stopOnboardingPoll()`. Bails when `onboardingStep` moves off `sync` or after a 30-min deadline. Restarting onboarding kills the prior poll first instead of stacking.

**setRecSource cache→library mismatch**
- The "📚 My Library" button used to call `setRecSource('cache', this)` while sending `source=cache` to the API — but the backend's `source=cache` path just reads `CachedRecommendation`, while the intended UX (library-typed recs) needs `source=library`. Switched the button to `setRecSource('library', this)` and updated the active-state CSS check accordingly.

**sendFeedback actually records feedback now**
- `/api/chat/message` (streaming) now flushes the new `ChatInteraction` row before yielding the final SSE frame, then emits `{"done": true, "interaction_id": <id>}` so the client can tag the rendered message bubble.
- Frontend reads `interaction_id` from the done frame, stores it on the `.fb` container's `dataset`, and only renders the 👍/👎 buttons if we actually have an id (no more lying-in-place dead buttons).
- `sendFeedback` POSTs to `/api/chat/feedback` with `{interaction_id, feedback}`. Optimistic UI on click, rolls back on error so the user knows it didn't save. Sibling button disabled to prevent double-voting.
- `/api/chat/feedback` now raises 404 instead of returning 200 with `{status:"not_found"}` when the row doesn't belong to the user — matches REST conventions and lets the frontend's catch branch fire.

---

### Pass 6b — Notifications pane (`0b36d46`)

The 13 proactive triggers were all-or-nothing: there was no way to turn just `night_owl` off short of stopping the scheduler.

**Backend (`proactive_messages.py`)**
- New `TRIGGER_TYPES` catalogue: list of `{type, label, description}` for every detector. Order matches priority in `_run_all_triggers`.
- `TRIGGER_TYPE_NAMES` set used to validate POST updates.
- `get_disabled_triggers(user_id)` / `set_disabled_triggers(user_id, set)` persist a JSON list under `app_state['notif_disabled:user_id=<id>']`. Empty/missing = all enabled. Unknown trigger names dropped on write so a stale UI can't poison storage.
- `_run_all_triggers` takes a new optional `disabled` set and skips matching trigger types alongside the existing `recently_fired` skip.
- `check_and_generate_messages` reads the user's disabled set once before the slot loop and forwards it.

**API (`routers/users.py`)**
- `GET /api/users/me/notification-preferences` → returns the catalogue with each item annotated `enabled: bool`.
- `POST /api/users/me/notification-preferences` with body `{trigger_type, enabled}` flips a single toggle and returns the updated catalogue.

**Frontend**
- New Settings → Notifications sub-nav entry (visible to all users).
- `loadNotificationPreferences` fetches the catalogue, then `renderNotificationPreferences` draws one row per trigger with label, description, and an on/off checkbox.
- The toggle posts immediately on change, optimistic UI, rolls back visually if the server rejects.

---

## Architecture decisions made along the way

* **`discuss_context` model:** option **(e)** RAG-style context document. Server fetches the actual record by id, builds a `[CURRENT DISCUSSION CONTEXT]` block in the system prompt; client never forwards the trusted text. Schema accepts `kind` + (`proposal_id` | `message_id`).
* **Multi-user attribution:** cross-reference `/status/sessions/history/all` per sync. `User.plex_user_id` keeps the Plex.tv account id (set at OAuth); `WatchHistoryEntry.plex_user_id` carries the per-server `accountID` for the actual play. Re-attribution and orphan cleanup are explicit admin actions, never automatic.
* **Cache invalidation:** single shared `recs_invalidate_at` app-state key. Sync writes it when `synced > 0`, enrichment writes it when `processed > 0`. Recs and DeletionProposal both consult it on read; recs auto-regenerate, deletion proposals get a `stale: true` flag for the UI.
* **PIN flow:** PIN registered (PBKDF2 hash stored), but actual encryption-at-rest of taste vectors is deferred. The decision (server-side decrypt-on-each-request vs client-side encrypt-before-store) hasn't been made.
* **Settings authority:** admin-only routes are gated at the prefix level in `main.py` (`enrichment`, `process_monitor`) or via `require_admin_or_first_run` (`setup`). `tasks.cancel_task` and `users.delete_user` are individually admin-gated. Music pipeline is intentionally per-user.
* **Soft-delete users:** `users.delete_user` flips `is_active=False` instead of `DROP`. Spec'd by the project owner because watch history / taste / memories carry `user_id` FKs without `ondelete=CASCADE`; soft delete preserves the data so re-attribution works on re-enable.
* **Thread-isolated chat history:** `ConversationMessage.thread_id` keys conversations by topic. Free chat lives on `general`; each Discuss action gets its own thread. Legacy NULL rows fold into `general`.
* **`source='spotify'` is the canonical "this play happened on Spotify" marker** — even when `music_matcher` has linked the row to a Plex music ratingKey. Re-attribute and cleanup never touch these rows.
* **Re-attribute is best-effort by Plex retention.** Plex purges `/status/sessions/history/all` per its retention setting; older plays stay attributed to admin. UI copy tells the user this is expected, not a failure.

---

### Pass 9 — Spotify batch wiring + service hygiene sweep

**Spotify batch wiring**

The `lastfm_batch` field on `MusicStartRequest` only ever gated Phase 2 (Last.fm).
Phase 1.5 (Spotify genre enrichment via `enrich_music_genres_spotify`) had no
cap and pulled every unresolved Spotify-track-id in one go, ignoring whatever
the user had typed into the UI batch field. For libraries with hundreds of
thousands of plays this hammered the Spotify API for minutes per run.

- `enrich_music_genres_spotify(user_id, batch=200)` now caps unique track ids
  per run; remaining ids carry over via the `genres IS NULL` filter.
- `run_music_pipeline(user_id, batch=300)` forwards a single `batch` to BOTH
  Phase 1.5 and Phase 2.
- `MusicStartRequest` now accepts a unified `batch` field; the legacy
  `lastfm_batch` is kept as an alias on the same model for back-compat.
- `_run_music_pipeline` router callback renamed param `lastfm_batch` → `batch`.
- `scheduler._run_music_pipeline_bg` updated to the new signature.
- Frontend label changed from "Last.fm batch size" to "Batch (per phase)";
  payload key changed from `lastfm_batch` to `batch`.

**Resume verified.** Phase 1.5 selection query filters
`WatchHistoryEntry.genres == None` so already-enriched tracks drop out of the
next run's set automatically. The stop button breaks cleanly between phases.

**Service hygiene**

- `episodic_memory.run_memory_decay` — added a time-based path. Previously the
  function bailed when the user had ≤ 500 memories, so genuinely stale rows
  (180+ days, importance < 0.3, never accessed) accumulated indefinitely on
  small accounts. Both paths now run independently.
- `episodic_memory:160` — replaced bare `except:` with explicit
  `(JSONDecodeError, TypeError, AttributeError)`.
- `episodic_memory:466` — German `"wurde(n) dauerhaft geschützt"` user-facing
  string translated to English to match the rest of the codebase.
- `arr_client.request` — bounded 429-retry: was tail-recursive on rate-limit,
  unbounded on persistent throttling. Now `max_retries=3` with explicit loop;
  still raises after exhaustion.
- `arr_client` — Tautulli `apikey` no longer baked into the `endpoint` string
  (which is what `logger.error` formats on timeout). Now passed via a separate
  `params=` dict that aiohttp serialises onto the URL but that never appears
  in our log lines.
- `music_matcher.enrich_music_genres_lastfm` — defensive normalisation of
  Last.fm's `tag` field. The API sometimes returns a single tag as a bare
  `dict` instead of `[dict]`; iteration crashed on the dict. Now coerced to
  a list and each element checked with `isinstance(t, dict)`.
- `proactive_messages.generate_proactive_message` — model fallback now triggers
  on ANY non-200 response (was 404-only) AND on empty-content 200 responses
  (which previously got stored as empty messages).
- `verification_session.start_verification_session` — guard against an empty
  LLM rephrase response. Previously an empty string overwrote the original
  question text and the user got a blank message.
- `routers/chat._check_verification_response` — atomic claim-then-process
  pattern. UPDATE … WHERE read=False returns rowcount 1 only for the first
  caller; concurrent retries see rowcount 0 and bail. Closes the duplicate-
  memory window on retry.
- `setup_wizard.write_env` — chmod 0600 on the written `.env` (POSIX only;
  Windows raises OSError on `os.chmod` and we swallow it). The file contains
  JWT_SECRET, API keys, Plex tokens — should not be world-readable on shared
  Linux hosts.
- `routers/music.py` — module docstring translated from German to English.

13/13 tests still pass. All modified modules import clean.

---

### Pass 15.2 — Single-word title strict-prefix guard

User test: "tell me about ghosts (2019)" → cascade-15.1 dropped music
correctly, but TMDB movie endpoint had no exact "Ghosts" 2019 entry,
so the unfiltered fallback returned "Inner Ghosts" — and
`_titles_close_enough` happily accepted it because "ghosts" is a
substring of "inner ghosts" with length-diff 1.

Fix: when the query is a single word, the substring must appear at the
START of the candidate (followed by space, colon, or open-paren). Mid-
candidate hits are rejected and fall through to the fuzzy-score path
(which has its own threshold and won't accept noise).

Verified:
- `Ghosts` vs `Inner Ghosts` → reject ✓ (was the actual bug)
- `Ghosts` vs `Ghosts of Mars` → accept ✓ (query at start)
- `Ghosts` vs `Ghost Story` → reject ✓ (partial word, not full "Ghosts")
- Multi-word queries: unchanged behaviour (existing length-diff path).
- Short-query (≤4 char) and colon-suffix guards from earlier passes:
  unchanged and still apply first.

13/13 tests pass.

---

### Pass 15.1 — Cascade year-aware + sharper NO INVENTION rule

User test: "tell me about the tv show ghosts from 2019" → cascade
exhausted, anchor fired — but the curator answered with correct
TV-show details from training memory anyway. Antwort stimmte
zufällig, aber unter einem `NO VERIFIED METADATA` anchor sollte das
Modell die Daten-Lücke explizit melden statt aus prior knowledge zu
schöpfen.

Plus an earlier turn "Ghosts (2019)" matched the music domain because
Music sat at the cascade tail and a fuzzy "Ghosts" band entry in
MusicBrainz won the slot — TMDB show / movie didn't have a tight match.

**A — Year-aware cascade.** When `year_hint` is set AND no music keyword
is in the query, music drops out of the cascade entirely. Year-tagged
queries are virtually always film/tv/anime; pulling MusicBrainz on
"Ghosts (2019)" risks a fuzzy band match standing in for what should
be a show lookup. Music keyword still promotes music to top regardless
of year.

Verified:
- "tell me about Ghosts from 2019" → `[movie, show, anime]` (music gone)
- "the album Vessel from 2023 by Sleep Token" → `[music, movie, show, anime]`
  (music keyword wins)
- "tell me about King Crimson" → `[movie, show, anime, music]` (no
  year, music stays at tail as before)

**B — NO INVENTION rule sharpened (without verbatim templates).** Rule 4
now explicitly tells the curator that **reciting training-memory facts
under an anchor IS hallucination** — not because the facts are wrong but
because the user has no way to know whether the answer came from the
verified pipeline or from the model's prior knowledge. The change is
deliberately about *framing the act* of recital-from-memory as
hallucination, not about prescribing literal phrases the curator must
emit.

Trade-off acknowledged: prompt-only enforcement is soft. If this still
leaks in real-world tests, post-stream anchor-validation (Option C from
the discussion) is the next step.

13/13 tests pass.

---

### Pass 15 — Scheduler missed-job replay + weekly DB VACUUM

Two infrastructure items the user flagged for the self-hosted reality:

**15a — Missed-job replay.** Curatarr is self-hosted; the laptop isn't
on 24/7. APScheduler skips missed firings by default — daily syncs and
weekly maintenance silently lose their slots when the app is off.

- Each scheduled job now wraps through `_tracked(job_id)`, persisting
  `job_last_run:<id>` in AppState after every successful run.
  Failed runs DON'T record — next startup catches up.
- `_startup_check` iterates over an explicit catch-up table, fires any
  job whose interval has elapsed × 1.1 grace factor.
- Catch-ups are CONSOLIDATED — a job that missed 5 daily windows still
  only runs once. Running 5× the same daily sync wouldn't produce 5×
  the value.
- `proactive_messages` (30 min interval) is intentionally excluded from
  catch-up — restarting would just dump too many of them.

**15b — Weekly DB VACUUM.** SQLite reclaims free pages only when
VACUUM runs explicitly. Without it, files grow even when row counts
stay flat (taste-vector rebuilds, metadata-cache churn, watch-history
rotation all leave tombstoned pages).

- New `job_db_vacuum` runs Sunday 04:30 (after the music pipeline at
  04:00). Targets `data/curatarr.db` and `data/cache/metadata.db`.
- For each file: `incremental_vacuum`, then `VACUUM`, then `PRAGMA
  optimize` to refresh query planner stats.
- NEVER deletes data — only reclaims free space.
- Logs old vs new size per DB so the impact is visible.
- Per-file isolated connections so a failure on one DB doesn't block
  the other.

Both items registered in the new tracked-job system so they participate
in catch-up replay.

13/13 tests pass.

---

### Pass 14.14 — German keyboard typo normaliser (ß → 0 near digits)

User test: "FBI: Most Wanted from 202ß" — ß sits next to 0 on German
keyboards, easy slip. Curatarr handled it gracefully (anchor fired,
curator asked for clarification), but the year hint was silently lost.

- `_normalize_typos(text)` regex-replaces ß with 0 ONLY when adjacent
  to digits ("202ß" → "2020", "2ß21" → "2021").
- ß surrounded by letters is untouched — *Straße*, *Maß*, *weiß* are
  preserved.
- Original user text still goes into conversation history + curator
  system prompt. Only the metadata-pipeline copy
  (`_extract_year_hint`, `_detect_media_in_query`) is normalised.
- Logged: `[chat] typo-normalize: 'X' -> 'Y'`.

---

### Pass 14.13 — Background tasks yield to curator + better timeouts

User log showed empty error messages from background tasks:

```
💥 [PROTECTION CHECK ERROR]:        (21s after start)
💥 [MEMORY EXTRACTION ERROR]:       (31s after start)
```

The 21s/31s deltas matched the 20s/30s httpx timeouts almost exactly.
Empty `str(e)` is typical of `httpx.ReadTimeout`.

Root cause: background `memory_extraction` + `protection_check` fired
right after `curator_done`, but the curator may still be streaming OR
a new curator turn may have started — both contest the same 14 GB VRAM
slot the summarizer needs. With the summarizer cold-loading + the
curator eviction-cycling, 20-30 s `ReadTimeout` was happening regularly.

Fix:
- Both background tasks now call `wait_for_curator()` before their
  Ollama POST. The priority event releases as soon as `curator_done`
  fires; the wait is usually instant.
- Timeouts raised: 20 s → 90 s and 30 s → 90 s.
- Error logger now includes `type(e).__name__` AND a `(no message)`
  fallback so empty exceptions still produce useful log lines.

---

### Pass 14.12 — `skip_llm_summary` now also skipped in music branch

Pass 14.8 added `skip_llm_summary` fast-path but only patched the
movie/tv/anime code path. The music branch always called
`summarize_with_small_llm` regardless of the flag.

Result: chat cascade for "King Crimson" / "Sleep Token" hit MusicBrainz
+ Last.fm in <1 s, then waited 5+ s on the summarizer LLM, exceeding
the 10 s cascade timeout. Both fell through to no-metadata anchor
despite the music APIs returning clean data.

Music branch now returns a raw-derived profile with the curator-
relevant fields (title, genres, country, similar_artists, top_albums,
bio, rating) without the LLM step. Cache is bypassed for save in
skip mode (same pattern as the non-music branch).

---

### Pass 14.11 — NO INTERNAL MONOLOGUE (curator stops dumping its reasoning)

User test showed the curator dumping its entire deliberation into the
response: rule citations ("Wait, the instructions say…"), self-talk
("Actually, looking at the metadata: Item shows…"), reconciliation
attempts ("My rule is: trust the current block…"). The
`ThinkTagStreamFilter` only catches `<think>`-tagged output —
reasoning-style models without explicit `<think>` markup leak straight
through.

Two-layer fix:

**Layer 1 — system prompt rule.**
New rule 6 NO INTERNAL MONOLOGUE explicitly forbids:

> Do NOT show your reasoning process. Do NOT quote, paraphrase, or
> refer to the rules above. Do NOT write 'Wait, the instructions say…',
> 'I must…', 'Let me think…', 'Actually, looking at the metadata…',
> or similar self-talk. Skip straight to the user-facing answer.
> Your deliberation happens silently; the user only sees the polished
> response.

Plus the `no_invention_rule` was condensed (was 5 sentences, now 1).
The previous version was so long that the curator started reciting
parts of it back to demonstrate compliance — making the rule the
problem it was trying to prevent.

**Layer 2 — post-stream safety net in `strip_think_tags`.**
Decoupled from the `LLM_THINK_TAGS` flag (was a no-op when False).
Always-on regex that detects monologue paragraphs starting with
`Wait,` / `Actually,` / `Let me` / `I must` / `Looking at` / etc.
AND containing rule-quote markers (`instructions` / `rule` /
`metadata` / `Item:` / `trust it` / `training memory` / `NO INVENTION` /
`context block`). Such paragraphs get dropped post-stream before the
response is persisted or shown.

Verified on the actual user-reported response: both
"Wait, the instructions say…" and "Actually, looking at the metadata…
Item shows a different director" paragraphs strip cleanly while
legitimate sentences are preserved.

13/13 tests still pass.

---

### Pass 14.10 — Filler trim, colon-suffix guard, topic-pivot trim, New Chat button

User test surfaced four issues — three bugs and one architectural
nice-to-have:

**Bug 1 — entity extraction kept trailing filler words.**
"tell me about the band sleep token then" → entity = "sleep token then".
The LLM extractor occasionally captures sentence trailers it shouldn't.
Fix: post-extraction trim loop strips trailing
`then/now/actually/please/already/recently/today/tonight/tomorrow/maybe/though/anyway`.
Loops because "Dune actually please" needs both removed; one-shot regex
only got the last filler. Verified on synthetic tests.

**Bug 2 — `_titles_close_enough` accepted colon-suffix substring matches.**
"King Crimson" matched "King Crimson: Deja VROOOM" (a TMDB-registered
concert film), so the cascade hit `domain=movie` for a band query. The
music pipeline (MusicBrainz / Last.fm) was never tried. Fix: when the
candidate has the form `<query>: <subtitle>`, reject the substring match
— the colon-suffix marks it as a *specific* sub-item (live concert,
season title, special edition), not the bare entity the user asked for.

**Improvement — auto-trim conversation history on topic pivot.**
User feedback: "wenn der chat festellt das es um ein neues thema geht
dieser autmoatisch das kontext fenster leert? denn hiernach ist die
tokengeschwindigkeit enorm in den keller gefangen". When the detected
title differs from the cached active_title for a thread, we now load
only `CONVERSATION_WINDOW_TOPIC_SWITCH = 4` recent messages instead of
the default 20. Two benefits:
1. Token generation stays fast — long histories crawl on a 22 GB model.
2. Stale assistant turns from the OLD topic (potentially full of wrong
   facts the curator confidently asserted) can't override the fresh
   `[VERIFIED METADATA]` block when there are fewer of them in scope.
A `🔄 Topic switched: X → Y` status frame is emitted so the user sees
what's happening.

**New "+ New" chat button.**
The frontend was missing a way to clear conversation history without
SQL. New button next to the chat input fires
`DELETE /api/chat/history?thread_id=general` and visibly resets the
chat log. Confirmation dialog before the wipe. The endpoint also clears
the in-memory `_thread_active_title` cache for the affected thread so
the next turn doesn't see a phantom topic-pivot from a wiped history.

**Out of scope (TMDB data quality):**
"Hard to Be a God 2013" lookup hits TMDB record 249720 with director
"Dmitry Tyurin" — that record may simply be a different film than the
2013 Aleksei German Sr. one users typically mean. Curatarr does the
right thing now (uses VERIFIED METADATA, mentions the mismatch
explicitly: "if you actually meant the Konchalovsky version, note that
the metadata I have is for Tyurin"). The Sokurov hallucination from
earlier passes is gone — replaced by honest acknowledgement of the
record we have. TMDB-side data correction would be the proper fix.

13/13 tests still pass.

---

### Pass 14.9 — Pre-stream status subnote ("backend is doing X")

Until this pass the user saw three animated yellow dots during the entire
1-3 s pre-stream phase (entity detection, cascade, memory retrieval) with
no signal about what was happening. Pass 14.8's fast-path made the wait
shorter but didn't make it visible.

**Backend** — pre-stream code accumulates a `pre_stream_status` list of
short event-strings as it runs. The streaming generator emits each as a
new SSE frame type:

```json
{"status": "🔍 Identifying media reference…"}
{"status": "📚 Looking up 'It' (1990)…"}
{"status": "✓ Found in show: 'It'"}
{"status": "🧠 Loading taste profile + memories…"}
{"status": "✓ 4 relevant memories loaded"}
{"status": "💭 Curatarr is thinking…"}
```

Status events for: discuss-context load, entity-detection start, lookup
start, cache hit, cascade hit, no-metadata anchor, memory retrieval,
memory count. The final "💭 Curatarr is thinking…" emits right after
`curator_start()` so the user sees the model is working.

**Frontend** — new `.thinking-status` style sits under the animated dots
with a subtle fade-in animation per status. Status events arrive in a
quick burst (because the pre-stream code is fast); a small JS queue
holds each one for ~350 ms before displaying the next one, so even an
instantaneous burst reads as a sequence. On the first user-visible
token, dots + status get cleared and replaced by the streaming text.

Trade-off: pre-stream events arrive at the END of the pre-stream phase
(when the generator first yields), not progressively as each step
finishes. Pre-stream is fast enough (1-3 s typically) that the user
still sees the sequence as feedback rather than as a delayed dump.
Truly progressive yielding would require moving the entire pre-stream
block into the generator — bigger refactor, deferred.

13/13 tests still pass.

---

### Pass 14.8 — Chat-cascade fast path (skip LLM summarisation)

User test for "It from 1990" showed the cascade getting the RIGHT TMDB
record (tv/19614 — Stephen King's 1990 miniseries, year-exact match)
yet timing out at 10s and falling through to anchor:

```
TMDB 'It' → year-exact match id=19614 (1990)
GET tv/19614/external_ids
GET tv/19614/credits
GET tv/19614/keywords
GET tv/19614 (details)
GET omdbapi.com (imdb_id=tt0099864)
[cascade show: timeout (10.0s) — moving to next domain]
```

Cause: `enrich_media_item` runs `summarize_with_small_llm(raw)` after the
API fetches — a small-LLM call on the merged metadata that re-tones the
synopsis, extracts hints, and produces a polished profile. That LLM step
adds 3-8 s on top of the 1-3 s API work. Combined with cold-cache
summarizer-load latency, the total exceeds the 10 s cascade window.

Cascade then retries the same query against `anime` (10 s timeout) and
`music` (10 s timeout), each going through the same fetch + LLM dance,
each timing out → 30 s of work for a "no metadata" anchor.

**Fix: `skip_llm_summary=True` parameter for the chat cascade.**

`enrich_media_item(skip_llm_summary=True)`:
- Runs all the API fetches as normal (TMDB / AniList / Jikan / OMDB / MB).
- SKIPS the `summarize_with_small_llm(raw)` call.
- Returns a raw-derived profile with everything the curator actually needs:
  title, year, director, cast, plot, genres, rating, country, runtime,
  studios, episodes_total, source_material.
- Does NOT save to cache (the partial profile would mask later full
  enrichments triggered by background workers).
- Cache LOOKUP still works — full profiles cached by previous bulk
  enrichments still get returned without re-fetching.

The chat cascade in `_enrich_with_cascade` now passes `skip_llm_summary=
True` for every domain it tries. Curator-facing latency drops from 8-15 s
per cold lookup to 1-3 s. The "It 1990" test should now hit `tv/19614`
within the 10 s window and return the correct TV miniseries data.

Background bulk enrichment workers are unaffected — they still call
`enrich_media_item` without the flag, get the full LLM-summarised profile,
and cache it. So cached entries stay rich; chat just doesn't WAIT for
the LLM step on cache miss.

13/13 tests still pass.

---

### Pass 14.7 — Curator idle-eviction with adaptive delay

Pass 14.6 set `CURATOR_KEEP_ALIVE=1h` so the curator stays warm between
chat turns. That solved the "curator reloads from disk every interaction"
problem, but introduced a new one: a user who stops chatting leaves the
curator pinned in 22 GB of VRAM for an hour, blocking background workers
that need the summarizer.

**Fix: explicit idle-eviction with adaptive delay (variant A+B from the
earlier design discussion).**

- After `curator_done()`, schedule a background task that evicts the
  curator after an idle period.
- New constants in `llm_utils.py`:
  - `CURATOR_IDLE_EVICT_SECONDS = 60` — default delay (read response,
    type follow-up).
  - `CURATOR_IDLE_EVICT_BUSY = 10` — short delay used when at least one
    summarizer call is queued at the moment the curator finishes.
- `wait_for_curator()` now increments/decrements a `_summarizer_pending`
  counter while it blocks. The eviction task reads the counter at firing
  time to choose between the two delays.
- `curator_start()` cancels any pending eviction — if the user comes
  back within the window, the curator stays warm.

**Steady-state behaviours:**

- User chats actively, every 20-40s a follow-up: curator never evicted
  (each new turn cancels the pending eviction).
- User reads long answer, takes 90s before next chat: evicted at 60s,
  next turn pays one curator reload (~10-15s), then 60s window resets.
- User chats while enrichment is running: chat done with summarizer
  calls queued → 10s eviction → background drains quickly. User comes
  back later → curator reload, fresh 60s window.

**Defensive:** `RuntimeError` from `asyncio.create_task` (no running loop,
e.g. sync test harness) is caught; the task simply isn't scheduled and
Ollama's own `keep_alive` takes over. `_curator_evict_task` cancellation
during `_evict_model`'s polling phase is safe — `keep_alive=0` was
already sent, Ollama unloads regardless of whether we wait for the
confirmation.

**Backlog item added:** Frontend typing-heartbeat would extend the
eviction window based on actual user typing, not just "time since last
curator response". Skipped for now; the 60s default is good enough for
the common case.

13/13 tests still pass.

---

### Pass 14.6 — Model lifetime tuning + smart eviction skip

The user observed the curator (22 GB) being aggressively evicted between
chat turns and reloaded from disk for every interaction. Cause: Ollama's
default `keep_alive` is 5 minutes per model, and the summarizer was being
loaded for background tasks (memory extraction, protection intent,
proactive messages, verification, entity detection) on its own 5-minute
keep_alive — so any background activity AFTER a chat turn would reload
the summarizer into VRAM, evicting the curator. Next chat turn: load the
22 GB curator from disk again. Repeat.

**Fix in three layers:**

**Layer 1 — explicit per-call lifetimes.**
New constants in `llm_utils.py`:

- `CURATOR_KEEP_ALIVE = "1h"` — applied to every curator call (chat
  streaming, recommendations engine, proactive message generation).
  Curator stays warm long enough to survive normal chat sessions.
- `SUMMARIZER_KEEP_ALIVE = "30s"` — applied to every summarizer call
  (entity detection, memory extraction, protection intent, verification
  rephrase + response analysis). Falls out of VRAM well before the
  user's next chat turn lands, so it doesn't hold the 14 GB the curator
  needs to be resident.

**Layer 2 — smart eviction skip in `curator_start`.**
Previously every chat turn called `_evict_model(summarizer)` which polled
`/api/ps` until the summarizer was confirmed gone — 0.5–2 s of latency
per turn even when the summarizer wasn't loaded at all. Now `curator_start`
first checks `loaded_models()` for the summarizer; if it's already absent
(because of Layer 1), the eviction call is skipped entirely.

Combined with Layer 1, the steady-state behaviour for normal chat use:

- User sends chat → curator already in VRAM (1h keep_alive) → no eviction
  poll → first token arrives in ~1-2 s instead of 10-15 s of "load
  curator from disk".
- After response, background tasks fire, summarizer loads briefly, runs,
  drops out 30 s later. Curator never evicted.
- Next chat turn: curator still warm.

**Layer 3 — backlog item kept honest.**
nomic-embed-text (0.5 GB) is still NOT managed by `llm_priority` —
explicitly marked as out of scope in the previous pass. Too small to
matter against the 22 GB curator + 14 GB summarizer dance.

13/13 tests still pass.

---

### Pass 14.5 — Cache invalidation, conversation-history override, 10s cascade

Pass 14.4 test results showed three lingering issues. All three turned out
to be cases where a Pass-14-level fix was *correct in isolation* but
neutralised by a layer above it.

**Bug 1 — `_titles_close_enough` strict mode never triggered.**
The Pass 14.4 short-query guard ("It" no longer matches "Strike It Rich")
verified perfectly in unit tests. But the user still saw "Strike It Rich"
injected as context. Cause: the **MetadataCache** had stored the false
match from before the fix landed, with a TTL of 7 days. Same `cache_key`
(`title="It", media_type="movie", year=1990`) → cache hit → stale data
served, the new logic never executed.

Fix: cache schema-version prefix. `MetadataCache._CACHE_VERSION = "v2"`
gets prepended to every key. Old "v1" entries still sit in the SQLite
file but become invisible — any new lookup goes through fresh API calls.
`cleanup_expired()` will sweep them out as their TTLs lapse. Bumping
`_CACHE_VERSION` in the future invalidates the whole cache without a
DB migration.

**Bug 2 — Curator ignored fresh metadata, deferred to its own old answer.**
For "Hard to Be a God 2013" the cascade hit movie domain, hidden context
was injected with the year-mismatch note + Aleksei German as director —
and the curator STILL answered "2014, Sokurov, no 2013 version exists".

Cause: conversation history (`CONVERSATION_WINDOW=20`) contained the
curator's earlier wrong answer from before the metadata pipeline
improved. With both signals present (fresh `[VERIFIED METADATA]` block
in the system prompt + old wrong assistant turn in conversation history),
the LLM trusted continuity — i.e. its own previous statement — over the
new authoritative block.

Fix: extension to the NO INVENTION rule:

> ABOVE ALL: If a [VERIFIED METADATA - USE THIS, IT IS REAL DATA] block
> is present in this turn, TRUST IT over your own previous statements
> in this thread. Conversation history may contain earlier answers of
> yours that were wrong (the metadata pipeline has improved). The
> CURRENT block is always more authoritative than what you said five
> messages ago. If the current block contradicts an earlier statement,
> silently correct yourself using the current block — do not loop on
> "no other version exists".

This is the prompt-layer counterpart to the data-layer cache-version
fix. Old curator answers in conversation history can no longer override
fresh metadata.

**Bug 3 — Cascade per-domain timeout 6s still too short.**
Texhnolyze hit AniList + Jikan in ~1s, but `summarize_with_small_llm`
inside `enrich_media_item` adds 3-5s of LLM-summarisation latency on
cold-cache items. 6s clipped that mid-summary, leaving the user with
no metadata even when both APIs had responded successfully. Bumped to
10s per domain. Worst case 40s for a 4-domain exhaust (rare); typical
primary hit still 1-3s.

13/13 tests still pass.

**Manual cleanup if you have stale data:**
The Pass 14.5 cache-version bump invalidates old entries automatically.
If you also want to clear the false `protected_media` row from earlier
testing, use the SQLite CLI (the Settings → Protected Media UI is still
on the backlog):

```sql
DELETE FROM protected_media WHERE title LIKE 'Hexen%';
DELETE FROM protected_media WHERE title LIKE 'Crimson red Datendieb%';
```

---

### Pass 14.4 — Six bugs from real-world testing

After Pass 14.3 the user ran the full test matrix and uncovered six
distinct issues, all real:

**Bug 1 — Title-match too liberal on short queries.**
"It" + year=1990 fell through TMDB's year-filter with zero results, then
the unfiltered fallback matched "Strike It Rich" via substring containment
(`"it" in "strike it rich"`). `_titles_close_enough` now requires an
exact match for queries ≤ 4 characters — substring-matching on tiny
queries produces more noise than signal.

**Bug 2 — Curator ignored verified metadata.**
For *Hard to Be a God 2013* the cascade hit movie domain in 8s, hidden
context was injected with the right director — and the curator STILL
replied "NO VERIFIED METADATA AVAILABLE … 2014 … Sokurov". Two
contributing causes:
  - Block header was the neutral "[HIDDEN METADATA CONTEXT]" — the
    LLM interpreted it as ambiguous. Now reads "[VERIFIED METADATA -
    USE THIS, IT IS REAL DATA]".
  - Year mismatch wasn't flagged: user asked about 2013, TMDB record
    is 2014. The model loops on "no 2013 version exists" because it
    can't tell those are the same title. New ⚠ YEAR NOTE block in
    the hidden context says "user asked YYYY, our record is YYYY,
    they're the same title — don't claim no version exists".

**Bug 3 — Music cascade gated behind keywords.**
"tell me about King Crimson" cascaded movie → tv → anime → exhausted,
never tried MusicBrainz, because the message had no `band`/`music`/
`artist` keyword. Music is now ALWAYS the tail of the cascade. When a
music keyword IS present, music gets promoted to head.

**Bug 4 — Cascade timeouts too aggressive on cold lookups.**
The 8/4/2 staged timeouts from Pass 14.2 worked for hot primary
domains but starved obscure secondaries. Texhnolyze: TMDB tv hit at
id=8838, AniList query in flight, killed by the 4 s secondary cap.
Switched to flat 6 s per domain. Worst-case full cascade: 24 s
(rare); typical primary hit: 1-3 s.

**Bug 5 — Regex title extraction failed on lowercase + extra phrases.**
Pass 14.1 patterns required `[A-Z]` as first captured char — which the
`IGNORECASE` flag already made redundant — and only covered 5 trigger
phrases. Lowercase queries like "tell me about vessel" extracted
nothing. Patterns now have no case requirement, plus added: "what do
you think about", "what is X", "what's X", "have you heard of X",
"erzähl mir über X", and "the album/track/song/film/show X".
Also: "by Y" suffix is now trimmed ("the album Vessel by Sleep Token"
→ "Vessel").

**Bug 6 — Thinking-dots vanished before first token.**
Frontend cleared `thinking.textContent = ''` *before* entering the
SSE read loop, so the dots disappeared the moment the response
started but before any token arrived. New behaviour: dots stay
visible until the FIRST `data.token` event, then get replaced by
the streaming text. Empty-stream fallback shows "(no response)".

Verified all six against synthetic tests:
- "It" no longer matches "Strike It Rich"
- "tell me about King Crimson" cascades into `music` tail
- "tell me about vessel" / "what's Inception" / "erzähl mir über
  Texhnolyze" all extract correctly via regex fallback
- year-mismatch note emits when user-hint differs from fetched year
- 13/13 tests still pass.

---

### Pass 14.2 — Hotfix: cascade timeout + protection-intent false-positives

Pass-14 test results revealed two real bugs:

1. **Cascade flat 3s timeout was killing every cold lookup mid-pipeline.**
   `enrich_media_item` runs a multi-stage pipeline (TMDB search → details
   → external_ids → credits → keywords → AniList → Jikan). For "Hard to
   Be a God 2013" the log shows TMDB **found** id 249720 and was 4 calls
   into fetching its full record when the 3s timeout fired and we wrote
   it off as "no metadata". Same story for *Dune*, *Inception*, *It 1990*
   — all would have been found, all timed out.

   On top of that, `asyncio.ensure_future(...)` was starting a NEW
   `enrich_media_item` call as a background task on every cascade
   timeout, so we ended up running the same TMDB / AniList / Jikan
   pipelines **three times** per chat turn (once per cascade domain).
   We were DDOS-ing our own metadata providers.

2. **Protection-intent detector fired on simple `tell me about X` queries.**
   Asking "Tell me about Hexenkönigin und der Datendieb" wrote the title
   to the user's `protected_media` table with the reason "user wants to
   keep the title from deletion" — pure false positive from the small LLM
   reading the message too liberally.

**Fixes:**

- Cascade timeouts now staged: 8s for primary domain (full pipeline gets
  to run), 4s for secondary, 2s for tertiary (cache-only effectively).
  Common case (hit on primary): same fast latency. Cold cache + correct
  domain: now actually returns the data instead of dropping it on the
  floor at 3s.

- No more `asyncio.ensure_future` background-fire on cascade timeout.
  The `MetadataCache` inside `enrich_media_item` already saves the
  result on the next user turn — the background fire was just adding
  duplicate API traffic without the user ever seeing the result.

- Protection-intent prompt rewritten with explicit positive AND negative
  examples: `tell me about X`, `what about X`, `do you know X`, `I like
  X` are all now flagged as NO_ACTION. Only explicit keep/save/protect
  language with a specific title triggers the action.

**Cleanup:** if your `protected_media` table picked up a false-positive
entry from earlier testing (e.g. *Hexenkönigin und der Datendieb*), it's
still there. Removing it via Settings → Protected Media or:
```sql
DELETE FROM protected_media WHERE title = 'Hexenkönigin und der Datendieb';
```

13/13 tests still pass.

---

### Pass 14.1 — Hotfix: regex title fallback + memory log noise

Test follow-up to Pass 14: title hint detection regex fired correctly on
"Tell me about Hexenkönigin und der Datendieb" and "tell me about a movie
called Hard to Be a God from 2013", but the curatarr-summarizer MODE 6
LLM call returned nothing usable for either query. Result: log line
`[CHAT] Title hint detected but no entity extracted`, no metadata
fetch attempted, anchor never injected → curator filled the gap from
training memory ("Sokurov" — wrong, again).

**Root cause:** entity extraction depended entirely on the small LLM's
JSON output. For obscure / non-English / niche titles the model
(a) returns a different dict shape than we parse, or (b) emits an
empty string entirely.

**Fix:** two-pass extraction.

1. LLM (MODE 6) tries first.
2. If that returns nothing usable, regex-fallback runs on the user's
   literal message: quoted phrases, "tell me about X", "called X",
   "kennst du X", "was hältst du von X". Trims trailing year mentions
   and filler words.

Verified on the user's actual failing queries — all extract correctly
via the regex fallback now.

Plus: dict-shape parser extended to handle more LLM output schemas
(`output`/`result`/`data`/`extracted` wrappers, `media_title`/`name`/
`entity`/`value` keys, single-item lists).

**Memory extraction log noise:** the `LLM returned dict with no usable
shape; skipping` warning was firing on every chat turn because the
summarizer correctly emits `{}` or `{"facts": []}` when there's
nothing timeless to extract from a "tell me about X" query. That's
not an error — it's the model behaving as instructed. Demoted to
debug level, plus added more wrapper-key recognition (`results`,
`extracted`, `output`, `preferences`) and explicit handling for
empty list/dict cases.

13/13 tests still pass.

---

### Pass 14 — Domain-aware metadata + title-change cache + cascade

Triggered by Pass-12 testing: anchor worked for fully unknown titles, but
three regressions surfaced under realistic load:

1. **Curator hallucinated director for *Hard to Be a God 2013*** — said
   "Sokurov", correct answer is Aleksei German Sr. Cause: TMDB returned
   the right film, but `_build_hidden_context` only injected
   `rating + genres + synopsis + year` into the system prompt. Director
   and cast were silently dropped, so the model filled the gap from
   training memory with the closest russian-arthouse-director it knew.

2. **First-turn-only entity detection blocked re-anchoring on title pivot** —
   "tell me about It from 2017" answered "NO VERIFIED METADATA" then
   immediately self-corrected ("That is a lie"). Cause: the chat-UX
   commit gated entity detection on `_thread_has_history`. Once a thread
   had any history, follow-up turns skipped lookup entirely, even when
   the user pivoted to a brand-new title.

3. **Domain default = "movie", TV miniseries silently lose** — "It from
   1990" hit `/search/movie` with year=1990, got nothing, anchor fired —
   but the title exists in `/search/tv`. We never tried the second
   endpoint.

Plus the user reminder that **music and anime are also valid query
targets** with completely different metadata field shapes.

**14a — Domain-aware `_build_hidden_context`**

Helper now branches on domain (`movie | tv | anime | music`) and injects
the right field set per type:

- movie/tv: director/creator, cast (top 5), country, runtime, original
  title, seasons/episodes (TV only)
- anime: studio, director, episodes, format, source material
- music: artist, country, similar artists (top 5), top albums (top 3),
  active years, bio excerpt

Fields that are missing from the data dict render as
`(not in our database)` instead of being dropped. This gives the NO
INVENTION rule something concrete to anchor against — the model sees
"we checked, we don't have that field" and can't pretend the gap is a
training-memory placeholder.

**14b — Title-change-aware entity detection**

Replaces the `first_turn` gate with a smarter cache:

- `_thread_active_title: dict[thread_id, (title, data, domain)]` — in-memory
  per thread, survives until server restart.
- `_looks_like_title_introduction(query)` — cheap regex check for hint
  patterns ("tell me about", "what about", "kennst du", quoted phrase,
  multi-word capitalized phrase). Runs on every turn, no LLM call.
- Hint present + detected title differs from cache → re-fetch via
  cascade, replace cache.
- Hint present + matches cache → reuse cached metadata.
- No hint + cache present → reuse cached metadata (follow-ups stay fast).
- No hint + no cache → proceed without metadata anchor (general chat).

**14c — Multi-domain enrichment cascade**

`_domain_cascade(message)` returns a sorted list of domains to try, with
the strongest keyword-hit domain first. `_enrich_with_cascade(title, year,
domains)` iterates: 3-second timeout per domain, returns on first hit.
Music is gated behind explicit music keywords because the MusicBrainz
lookup is slow enough that we don't want to fire it on random TV chatter.

Logs show the cascade decision so production debugging is one grep away:

    [chat] cascade hit on domain=tv for 'It'
    [chat] cascade exhausted for 'Hexenkönigin' (year=None, tried=['movie','show','anime'])

**14d — NO INVENTION rule sharpened for partial-data case**

Previous wording covered "block missing entirely" but didn't address
"block present, individual field says (not in our database)" — which is
the exact case that produced the Sokurov hallucination. New rule
explicitly enumerates field categories per domain (director, creator,
studio, artist, episode count, source material) and says "(not in our
database)" means "we don't have it, do NOT fill from training memory".

13/13 tests still pass.

---

### Pass 13 — VRAM eviction race + curator health probe

Triggered by an `ollama ps` showing all three models (curator 22 GB,
summarizer 14 GB, nomic-embed 0.5 GB) at 100% CPU after a chat request.
Diagnosis: the eviction signal was fire-and-forget, opening a race where
the curator load fight the still-resident summarizer and OOM-fell-back
to CPU for everything.

**Blocking eviction**

`_evict_model` now POSTs `keep_alive=0` AND polls `/api/ps` until the
model is no longer listed. Match is by model base name (without `:tag`)
since Ollama may report either form. 8 s timeout — past that we proceed
anyway and let the health probe surface the fallback.

**Curator VRAM health probe**

`check_curator_vram_health(model)` reads `/api/ps` and returns severity
based on `size_vram / size`:

- ≤ 13% on CPU → `ok` (small spillover for context buffers is normal)
- 13–50% on CPU → `moderate` ("response will be slower than usual")
- ≥ 50% on CPU → `severe` ("response will take much longer; restart Ollama")

**SSE warning frame**

The chat streaming generator now spawns a 2-second-delayed health probe
in parallel with the curator request. When the result indicates a
fallback, a `{warning, severity}` SSE frame is emitted **once**, just
before the first token, so the user sees the banner immediately when
the answer starts arriving — not at the end.

Frontend renders the warning as a small banner (amber for moderate,
red-tinted for severe) above the streaming bubble.

**Timeout raised**

Curator HTTP timeout went from 120 s → 600 s. With partial-CPU fallback
the response can take 5-10 minutes; we'd rather wait it out than 504
the user mid-stream.

**Notes**

- `nomic-embed-text` (0.5 GB) is intentionally NOT managed by
  `llm_priority`. It's small enough that its presence in VRAM is
  acceptable noise; the eviction logic targets only the summarizer
  (the 14 GB one that actually competes with the curator for VRAM).
- The probe is best-effort — if Ollama hasn't loaded the curator yet
  by 2 s (cold start, very large model), `severity` is `not_loaded`
  and no warning is emitted. The user just sees normal slow first-token
  latency.

13/13 tests still pass.

---

### Pass 12 — Anti-hallucination + memory hygiene

Triggered by a chat session where the curator confidently invented a Larry
Clark 2001 film when asked about *Jesus Shows You the Way to the Highway*
(actually a 2019 Miguel Llansó film), then doubled down with arrogance and
an ad-hominem about the user's "broken premise" before finally caving and
inventing a *different* wrong director (Ilmar Raag). Same session: a FNAF2
discussion bled the Jesus-film context across thread isolation, surfacing
that Pass-3.5 only isolated `ConversationMessage`, not `EpisodicMemory`.

Five distinct bugs identified; this pass addresses four (memory cross-thread
bleed, addressed as a backlog item — see "Memory layer cross-topic bleed").

**A — Anti-hallucination anchor (data layer)**

`_build_no_metadata_anchor(title)` injects an explicit "NO VERIFIED METADATA
AVAILABLE" block into the system prompt when enrichment returns nothing.
Previously, an empty `enrichment_data` dropped a silent gap and the curator
fell back to training data. Now both the free-chat path and the discuss path
call the anchor on cold-cache / no-match.

**B — Year-hint disambiguation in TMDB search (Bug 4)**

The leading cause of the Jesus-film fiasco: `_tmdb_search_and_fetch` did a
plain `?query=title` and took the first close-enough match. Two films with
the same/similar title in different years would fight over the slot and the
older / more popular one usually won — wrong record → hallucination magnet.

- `_extract_year_hint(query)` regex-pulls a 4-digit year (1950–2049) from
  the user's free-form message.
- `_detect_media_in_query` now returns `(title, year)`.
- `enrich_media_item` and `fetch_and_prepare_raw` accept `year=` and forward
  it to `_tmdb_search_and_fetch`.
- `_tmdb_search_and_fetch` adds `&year=N` (movie) / `&first_air_date_year=N`
  (TV) to the TMDB call. Two-pass match: pass 1 prefers year-exact hits;
  pass 2 falls back to title-only match with a logged warning when the
  found year doesn't match the hint.

**D — Memory extraction: user-only scope + prompt refresh (Bug 2)**

Memory extraction used to feed both `User said:` and `Assistant said:` into
the small LLM. When the assistant hallucinated, the user's polite "ok thanks"
could indirectly cement the hallucination as a long-term preference. Worse:
arguments with the curator were getting extracted as taste signal.

- Assistant response is now stripped from the extraction prompt.
- Prompt explicitly tells the model "the assistant's reply is intentionally
  NOT shown — it sometimes hallucinates and we don't want pollution."
- Two new rules: rule 6 forbids storing factual corrections as preferences
  ("actually it's 2019 not 2001" is not a taste signal); rule 10 says
  "when in doubt, prefer [] over a vague memory."
- Rule 4 explicitly tolerates typos and informal grammar — extract INTENT,
  not verbatim quote.

**Bonus — NO INVENTION rule in system prompt (prompt layer)**

Paired with the anchor, the system prompt now carries an explicit rule 4:

> If [HIDDEN METADATA CONTEXT] is missing OR explicitly says
> "NO VERIFIED METADATA AVAILABLE", you MUST NOT invent factual claims …
> It is always better to say "I don't have verified data on that" than to
> confidently make something up. Your value is in sharp opinion ON FACTS,
> not in fabricating the facts themselves.

Data-layer anchor + prompt-layer rule together stop the "elite expert who
quietly fills the gaps with whatever sounds plausible" behaviour.

**Open / out of scope this pass**

- Memory-layer cross-topic bleed (Bug 1, Bug 5): `retrieve_memories` is
  user-globally vector-searched; thread-scoping memories would lose
  cross-topic taste signal, and just dropping "argumentative" memories is
  fragile to detect. Backlog candidate; needs more thinking.

13/13 tests still pass.

---

### Pass 11 — Music pipeline cascade + scope tightening

**Spotify → Last.fm failover cascade**

Phase 1.5 now bails out early when Spotify hits a hard rate-limit instead of
sitting in 30+ second backoffs that block the whole pipeline:

- `_batch_get` returns `(items, rate_limited)`. The flag flips when:
  - two consecutive 429 responses come back, OR
  - a single Retry-After exceeds 60 seconds.
- `resolve_track_genres` propagates the flag and skips the artist hop if
  hop 1 already triggered a hard rate-limit.
- `enrich_music_genres_spotify` returns `spotify_rate_limited: True` in its
  stats dict and ends the phase, instead of grinding through the rest of the
  unique-track-id pool.
- `_run_music_pipeline` logs the cascade at WARNING level and surfaces it in
  task-monitor and progress state so the UI can reflect "Spotify rate-limited
  at N tracks — falling through to Last.fm".

**Phase 2 scope: Spotify-source only**

Last.fm Phase 2 used to run on every `genres IS NULL` music row regardless of
source. New filter is `source='spotify' AND genres IS NULL`. Rationale:
- Spotify-streams have clean artist+title from the import → Last.fm lookups
  hit reliably.
- Plex-rip rows get their genres from Plex itself in Phase 1 and from the
  regular metadata enrichment pipeline later. Last.fm was producing noise
  for that pool.
- Phase 2 is now positioned as the safety net for the Spotify cascade, not
  a catch-all genre source.

This means a Plex-rip-only library will see 0 Last.fm activity — by design.

**Diagnostic logging**

- Phase 2 start log includes the unique-track count and the configured batch.
- Phase 2 end log reports `iterations_done/planned, stopped_early=true|false`
  so the next time the loop terminates earlier than expected it shows up
  directly in the log line — no more "queried=4 of 56307, why?".

13/13 tests still pass.

---

### Pass 10 — Chat output cleanup + feedback removal

**`\n\n` literal text in curator output**

The chat system prompt contained:
```
- You MUST insert double line breaks (\\n\\n) before and after EVERY heading…
```
In Python this renders as the four characters `\`, `n`, `\`, `n` — and several
LLMs (Qwen, Dolphin) imitated the escape sequence literally, writing `\n\n`
into the chat as visible text instead of pressing actual newlines. Replaced
the rule with plain English ("Separate paragraphs with a single blank line")
plus an explicit "do NOT write the literal characters \\n" guard.

**Memory-extraction defensive parse**

`extract_long_term_memories` bailed out with a warning when the LLM returned
a dict instead of the expected list. Now coerces `{"facts": [...]}` /
`{"memories": [...]}` wrappers and bare single-fact dicts into list form
before iterating — fewer dropped extractions on noisier model output.

**Thumbs-up/down feedback removed**

The 👍/👎 buttons on assistant chat bubbles wrote `chat_interactions.feedback`
but no code path ever read the column — write-only with no learning loop.
The visible promise of "your feedback shapes the curator" was a lie. Removed:
- Buttons + `sendFeedback()` JS function in `frontend/index.html`
- `.fb-btn` and `.msg.assistant .fb` CSS rules
- `POST /api/chat/feedback` endpoint in `routers/chat.py`
- `ChatFeedback` schema + the schema barrel export
- The final SSE frame's `interaction_id` payload (no longer needed client-side)

`ChatInteraction.feedback` column is kept (deprecated comment) to avoid a
destructive migration. If feedback is ever wired back as an explicit style-
tuning channel — see backlog — the column can be reused.

---

## Open backlog

Everything below has been seen, scoped, or tried; none of it is currently in progress.

### Pass 7 — Concurrency & lifecycle

| Bug | File:Line | Detail |
|---|---|---|
| `llm_priority` race on `_active` / `_event` | `services/llm_priority.py:88-110` | Mutated without `asyncio.Lock` between an `await _evict_model` and `_event.clear()`; lost-wakeup risk → enrichment can deadlock indefinitely. `curator_done` is sync with no try/finally guarantee at call sites. |
| `chromadb_wrapper` module-import side effect | `vector_store/chromadb_wrapper.py:170` | `chroma_db = ChromaDBWrapper()` runs on import — opens `PersistentClient`, creates filesystem state at `settings.CHROMADB_PATH`, crashes the whole app if Chroma can't initialise. Should be lazy (factory + `lru_cache`). Plus `Settings(allow_reset=True)` and a public `reset()` method that wipes ALL collections — needs admin-gating or removal. |
| `scheduler.ensure_future` orphan tasks | `services/scheduler.py:111, 186, 229` | Tasks aren't retained anywhere; Python may garbage-collect them mid-run. Fix is the standard module-set pattern. |
| `enrichment_running` TOCTOU | `routers/enrichment.py:322, 342, 678` | Read-then-write is racy across two concurrent POSTs. Single-worker setup mitigates but doesn't eliminate. |
| Magic `200000` TMDB-ID heuristic | `routers/enrichment.py:736, 772` | TVDB-vs-TMDB heuristic with no documented origin; TMDB IDs already exceed 1 M for real movies, so legitimate IDs get treated as suspect. Needs investigation of the actual data path before fixing. |
| `tasks.py` SSE auth-fail returns 200 | `routers/tasks.py:79-82` | Single SSE frame `{'error':'auth_failed'}` with HTTP 200; should be 401. EventSource limitation around 401 needs to be considered. |
| `tasks.py` legacy JWT-in-query-string | `routers/tasks.py:65-71` | Frontend now uses tickets exclusively; the `?token=<JWT>` fallback is dead code that increases attack surface. Remove. |
| `enrichment.py` double `sqlite3.connect` to cache_path | `routers/enrichment.py:170-176` | `MetadataCache()` opens one connection; the route opens a second one for a LIKE query. SQLite-locked under load. Use `_mc.conn` directly. |
| `enrichment.py` `_db_write_with_retry` dead | `routers/enrichment.py` | Defined, never called; inline retry loops reimplement the same logic. Either use it or delete it. |

### Pass 8 — Multi-user polish

| Item | Detail |
|---|---|
| Music-pipeline AppState keys still global | `music_pipeline_running`, `music_pipeline_stop_requested`, `music_pipeline_progress` all single-key. With multiple users, A's pipeline blocks B's start and B's stop interrupts A's run. Namespace as `music_pipeline_running:user_id=<id>` etc. |
| `spotify_client` token cache global | `services/spotify_client.py:30-35` | Module-level `_token` shared across all client_id/secret pairs. With per-user Spotify configs, first caller's token wins. Key on `client_id`. |
| Re-attribute on user-login hook | Currently the admin must run re-attribute manually. Could trigger automatically (or offer a banner) the first time a non-admin user logs in. |

### Frontend hardening

| Bug | File:Line | Detail |
|---|---|---|
| XSS via `onclick` JSON-in-attribute | `frontend/index.html:1929, 1981, 2759` | `JSON.stringify(...).replace(/"/g,'&quot;')` embeds LLM-generated text into `onclick` handlers. Brittle escape, breakable by U+2028, backslashes in titles, etc. Switch to `data-*` attributes + `addEventListener`. |
| JWT in `localStorage` accessible to any XSS | Architectural | Combined with the onclick surface above forms a token-theft chain. Move to `httpOnly` cookie or accept the trade-off. |
| Frontend dead stubs | `index.html:1238` `toggleUserMenu()` empty; `2600+` `startEnrich`/`startEnrichForce`/`checkEnrichJob` legacy wrappers; `2600+` self-comment "Legacy - keep for compatibility" confirms. Remove or wire. |
| `respondToMessage` `data-msg` XSS surface | Server-generated content; flagged but lower-risk than the onclick handlers. |
| EventSource has no `last-event-id` recovery | Reconnect drops events emitted during the gap. Edge case but real. |

### Frontend typing-heartbeat for curator-eviction window

Pass 14.7 schedules the curator-evict timer based on "seconds since the
last curator call". That's a coarse proxy: a user who's reading a long
response and slowly typing a follow-up looks identical to a user who
walked away. The current 60s default is conservative-but-rough.

Cleaner solution: the chat input field emits a lightweight heartbeat
(`POST /api/chat/typing` with thread_id) on keypress. Backend extends
the eviction timer when it sees recent typing activity. Practical
result: a user typing for two minutes never gets the curator evicted,
but a user who stopped typing 30s ago does.

Sketch:

- Frontend: `oninput` debounced to 1s sends `{thread_id}` to a new
  router endpoint. Cheap (no LLM, just an in-memory dict update).
- Backend: `last_typing_at[thread_id] = now`. The
  `_scheduled_curator_evict` task checks this on wake-up; if last
  typing was within (delay - 5)s, push the eviction another 30s
  out and re-sleep.
- Cap: never extend more than N times (e.g. 5 minutes of continuous
  typing → still evict eventually). Prevents a stuck typing handler
  from pinning the curator forever.

Trade-off: slightly more complex eviction logic, plus a frontend
keypress handler. Skipping for now — the 60s default works fine for
the common case.

### Library add pipeline (Sonarr / Radarr / Lidarr)

The curator currently lacks any path to actually add titles to the user's
library. When a user replies "yes" to a recommendation, the curator was
hallucinating "Good. X (year) is added to your library." — pure fabrication;
nothing happened. Pass 14.2 added a NO LIBRARY ACTIONS system rule to keep
the curator honest in the meantime, but the real fix is to ship the
add pipeline.

**API endpoints** (per the user's pointer):

- **Sonarr v3** (`https://sonarr.tv/docs/api/`)
  - `POST /api/v3/series` — add series with rootFolderPath, qualityProfileId,
    monitored, languageProfileId, addOptions{searchForMissingEpisodes}
  - `GET /api/v3/qualityprofile`, `/api/v3/rootfolder`, `/api/v3/languageprofile`
    — to populate the add request body
  - `GET /api/v3/series/lookup?term=<title>` — verify the title exists
    in TVDB before adding
- **Radarr v3** (mirrors Sonarr)
  - `POST /api/v3/movie`, `GET /api/v3/qualityprofile`, `/api/v3/rootfolder`
  - `GET /api/v3/movie/lookup?term=<title>` — TMDB-backed search
- **Lidarr v1**
  - `POST /api/v1/artist` (artist-level add) or `POST /api/v1/album`
  - `GET /api/v1/qualityprofile`, `/api/v1/rootfolder`, `/api/v1/metadataprofile`
  - `GET /api/v1/artist/lookup?term=<name>` (MusicBrainz-backed)

**Backend work needed:**

- Extend `arr_client.py` with `add_series` / `add_movie` / `add_artist`
  + the supporting `lookup_*` and `list_*` helpers.
- New router `src/routers/library.py` exposing
  `POST /api/library/add` with `{title, year, media_type, tmdb_id, imdb_id, ...}`
  payload. Server resolves the *arr-target by media_type, picks the user's
  default rootFolder + qualityProfile (configurable via Settings), calls
  the *arr add endpoint, returns success / failure / queued status.
- Settings → Library: per-arr default rootFolder + qualityProfile picker
  (today's setup wizard captures only URL + API key).

**Curator integration:**

- The curator either (a) emits a structured action signal in the SSE stream
  (e.g. `{action: "add_proposal", title, year, media_type, tmdb_id}`) which
  the frontend renders as an "Add to Sonarr/Radarr/Lidarr" button, OR
  (b) we add real tool-calling (deferred Pass 15+ decision) so the curator
  can hit the add endpoint itself when the user explicitly approves.

Variant (a) is the safer first step: user always confirms before anything
gets added; LLM hallucinations can't sneak past a button click. Variant (b)
is more powerful but ties us to tool-capable models and bigger orchestration.

**Frontend:**

- "Add to library" button on chat-bubble level for any title the curator
  has discussed (when enrichment data is present, since we need a TMDB/
  TVDB/MB id to add cleanly).
- Settings → Library Defaults panel for rootFolder + qualityProfile per arr.

This is a substantive architecture pass, not a hotfix. Sequence it AFTER
the chat / metadata / recap core is fully verified — adding moving parts
to Sonarr/Radarr/Lidarr while the curator's reasoning still has gaps would
risk silent mis-adds (wrong year, wrong show variant) that are then a
pain to clean up.

### Incoming Library Audit (Sonarr / Radarr / Lidarr — pre-storage curator)

Counterpart to the existing Deletion Proposals: instead of recommending
removal of items already on disk, scan items **just added** to Sonarr /
Radarr / Lidarr and let the curator render a verdict BEFORE storage is
committed. User can drop the item from arr before any download work
happens — no wasted disk, no wasted bandwidth.

**Two data lanes per service:**

| Service | Recently downloaded | Awaiting download (empty) |
|---|---|---|
| Sonarr | `/api/v3/episodefile?dateAdded > now-N days` OR series with `episodeFileCount > 0` AND `series.added > now-N days` | `/api/v3/series` with `statistics.episodeFileCount == 0` AND `monitored=true` AND `added > now-14 days` |
| Radarr | `/api/v3/movie` with `hasFile=true` AND `movieFile.dateAdded > now-N days` | `/api/v3/movie` with `hasFile=false` AND `monitored=true` AND `added > now-14 days` |
| Lidarr | analogous (album-level + artist-level) | analogous |

**Reviewability score (decides whether the curator answers at all):**

| Signal | Pts | Reasoning |
|---|---|---|
| TMDB `vote_count >= 20` | +3 | Reviews exist, curator can pattern-match |
| TMDB `vote_count` 5–19 | +1 | Limited but present |
| `release_date <= today` (released) | +2 | Internet has data |
| `release_date > today + 60 days` | -3 | Far too early — no judgement possible |
| Plot summary > 200 chars | +1 | Enough substance to evaluate |
| OMDB has IMDB rating | +1 | Second source confirms data |
| Genre + cast present | +1 | Minimum metadata available |

- Score ≥ 4 → **reviewable** → curator generates verdict
- Score 2–3 → **borderline** → tentative take + auto-requeue in 4 weeks
- Score < 2 → **skip** → "Too new / unknown to judge. Re-checking in N days." (no curator call → token-thrifty)

Items that don't pass go into a **Re-evaluation Queue** and get auto-rescored
after 7 / 14 / 30 days as their TMDB metadata accumulates.

**UI (variant A — separate sidebar page):**
- Sub-nav entry "Incoming"
- Three tabs: **Recently Downloaded** | **Awaiting Download** | **Pending Review**
  (Pending Review = the re-evaluation queue, just for visibility)
- Per item-card: curator verdict + Keep / Drop from arr / Snooze (re-check in N days) / Override (force critique anyway)

**Trigger:** cron-job (default every 6h) PLUS manual "Scan now" button on
the page. No webhook integration needed for v1 — webhook would require
user-side arr config which is friction we don't want yet.

**Architectural prerequisites (share with Library Add Pipeline backlog item):**
- `arr_client.py` extended to read `series` / `movie` / `album` lists with
  filtering and statistics
- New `IncomingProposal` table (similar shape to `DeletionProposal`)
- Re-evaluation queue tracking — last_score, score_history, next_recheck_at

**Sequence after Library Add Pipeline ships** — the same arr-client work
is needed for both, so they're naturally bundled.

### Scheduler missed-job replay

The scheduler currently runs `apscheduler` (or similar) cron jobs — when
Curatarr is shut down, missed jobs are simply skipped. For a self-hosted
app that isn't 24/7 this means daily-sync and incoming-audit runs can
silently miss multiple windows.

**Required behaviour:**
- Persist last-successful-run timestamp per scheduled job (`scheduled_jobs`
  table or AppState row).
- On startup, compare `now - last_run` to the job's interval. If
  `delta > interval`, fire the job once with a "this is a catch-up run"
  flag.
- Catch-up runs should NOT cascade ("we missed 5 daily syncs, run 5
  syncs back-to-back") — one consolidating run is enough.
- Heavy jobs (music pipeline, re-attribute) should respect the existing
  `_resume_*_if_needed` startup hooks plus the missed-job logic.

### DB maintenance / VACUUM

After months of taste-vector updates, ChromaDB embeds, metadata-cache
inserts, watch-history rows accumulating, the SQLite files keep growing
even when row counts stay stable (free-list pages aren't reclaimed until
VACUUM runs). For a long-lived install the file size becomes
disproportionate to actual data.

**Required behaviour:**
- Cron job (weekly, off-hours) runs `PRAGMA incremental_vacuum` then
  `VACUUM` on:
  - `data/curatarr.db` (main DB)
  - `data/cache/metadata.db` (api_cache + media_items)
  - any other persistent SQLite files
- Optional: `PRAGMA optimize` after VACUUM to refresh query stats.
- Logging: report old vs new file size per DB so user can see the impact.
- NEVER delete enrichment data (rich metadata is expensive to re-fetch and
  stays valuable across recommendations) — VACUUM only reclaims free space.

**Pairs naturally with the Scheduler missed-job replay item** — same code
path that fires daily-sync can fire weekly-vacuum.

### Stats page + Monthly Recap + Yearly Audit (architectural feature)

A dedicated `/stats` page modelled loosely after stats.fm, plus two scheduled
recap reports per user. To be implemented after the deletion / chat /
proactive-message / metadata core is verified stable in production.

**Monthly recap (small, additive):**
- Top 5 tracks / artists / movies / shows / anime by play count this month
- Total hours per category vs. previous month (delta arrows)
- "More/less music compared to last month" type summary
- Persisted as `MonthlyRecap` rows so the UI can show a back-catalogue.

**Yearly audit (the monster):**
- Tier 1 metrics (deterministic, must-have):
  - Total watch-hours, distinct titles, top genres, top artists
  - **Stubbornness Index** — titles whitelisted/saved from deletion that
    haven't been touched in 90+ days. Killer metric: pure SQL, ruthless,
    emotionally aware.
  - Genre-reality check — taste-vector top genres vs. actual watch-hours
    top genres. Surfaces the gap between claimed and real preferences.
  - Graveyard (Variant A): a series counts as abandoned when the last
    watched episode is not the latest available AND no play in 60+ days.
    Films: started but viewCount=0 + viewOffset>0.
  - Wasted Lifetime — sum of hours invested in graveyard titles.
- Tier 2 metrics (moderate work):
  - Binge analysis — leans on existing `BINGE_EPISODE_THRESHOLD` /
    `BINGE_SESSION_HOURS` config. "X series consumed at >Y eps/session"
    versus normal-paced watches.
  - Trash-Tax — titles with multi-source rating < 5/10 watched to
    completion. Needs bulk pre-load from enrichment_cache.
- Tier 3 metrics (anecdote in final narrative, not stand-alone scores):
  - Music-genre top-5 vs. visual-media-genre top-5 (replaces the fuzzy
    "Metal-to-Anime ratio" idea — same vibe, computable)
  - Irony moments: explicit examples like "abandoned X after 12 min,
    finished Y the same day" — pulled from data, not its own metric

**Skipped from the original concept:**
- "Aesthetic Regression" with philosophical categories ("polite monsters"
  vs. "theatrical suffering") — would need per-title LLM analysis, too
  expensive and hallucination-prone.
- "Diktatur: a single recommendation" — UI-hostile, users want choice.
  Three recommendations, each tied to a specific audit finding, instead.
- Buzzword tonality in the metric descriptions — kept the curator's
  edge for the final narrative paragraph only; the metrics themselves
  display data crisply, not insultingly.

**Architecture sketch:**
- `src/services/recap_engine.py` — aggregations
- `MonthlyRecap` + `AnnualAudit` tables (per `user_id`, immutable once
  written)
- Cron job: month-end + year-end runs
- Final narrative generated by curator LLM with audit data as system
  prompt — stays in character but operates on facts.
- Frontend: dedicated sidebar entry, card-grid layout per metric, scrollable
  history for past recaps. Long-term: extend into a general stats page
  (stats.fm-like) covering watch-history beyond just recap windows.
- Multi-user mandatory — every metric scoped per Plex account.

### Memory layer cross-topic bleed

| Item | Detail |
|---|---|
| `retrieve_memories` is user-global, not thread-scoped | Pass 3.5 isolated `ConversationMessage` per thread, but `EpisodicMemory` is searched by user_id + vector similarity only. A heated argument about title A produces "general taste observation" memories without `metadata.title` set; those then surface in semantically-related discussions about title B. `_filter_memories_for_topic` only catches memories WITH a different title set — generic ones rush through. Two architectural options were discussed and both rejected as too costly: (a) thread-scope memories (loses cross-topic learning), (b) drop emotionally-charged memories (fragile to detect). Needs a third approach. |
| Conversation tonality bleed via long window | `CONVERSATION_WINDOW = 20` (10 user/curator pairs). When a previous topic ended in conflict, the aggressive style carries over to the next topic. Soft-reset on new title-entity detection is one option; reducing the window is another. |

### Curator style-tuning channel

| Item | Detail |
|---|---|
| Re-enable feedback as style-tuning channel | The thumbs-up/down chat-bubble UI was removed in pass 10 because it was write-only — `chat_interactions.feedback` was set but never read by any code path. Memory-extraction already covers taste signal; what's missing is a *style* signal (was the response too cautious / too long / off-topic?). Possible re-implementation: 👎 generates a `style_correction` `EpisodicMemory` with `importance=0.6` and feeds into the curator system prompt as a "previous response style was rated negatively" hint. Column kept on `ChatInteraction` to avoid a destructive migration. |

### Service-level hygiene

| Item | Detail |
|---|---|
| `episodic_memory` sessions held across `httpx` calls | `episodic_memory.py:114-209, 548-557` — DB sessions held over awaited HTTP calls. Move HTTP outside the `with`, re-open for the write. |
| Music_matcher loads all Spotify history into memory | `services/music_matcher.py:157-194` — `.all()` not `.yield_per()`. Memory bomb for large libraries. |
| Music_matcher empty-genres case excludes from phase 2 | `services/music_matcher.py:273-274` — tracks Spotify says nothing about get `genres = ""` and never get the Last.fm fallback. |
| `proactive_messages` forward-dated `created_at` | `now + timedelta(seconds=generated)` to force ordering. Filters with `created_at <= now` would skip them until time catches up. |
| `setup_wizard` overwrites entire `.env` on every call | Partial updates blank out fields not in the payload. |

### Encryption-at-rest activation

PIN flow registers the hash but doesn't yet encrypt anything. Two viable architectures:
* **Server-side**: client sends PIN with each crypto-touching request, server derives key and encrypts/decrypts. PIN-in-transit per request is the cost.
* **Client-side**: client derives key locally, sends ciphertext only. Frontend complexity is the cost.

This is a separate architectural pass, not a quick fix.

### Re-attribute & cleanup limits (informational, not bugs)

* Plex's `/status/sessions/history/all` retention purges old play events. Re-attribute can only assign accountIDs that Plex still remembers. Older plays stay on admin — expected, surfaced in the UI.
* Tautulli, if available, would have full history with per-account timestamps. A Tautulli importer is a candidate future feature for users who want full historical attribution.

---

## Branch state

Working tree is clean except for `add_col.py` (intentional). 44 modules import cleanly; 13/13 tests pass. The branch is local — has not been pushed to `origin`.

When this branch lands on `main`, the live database doesn't need any manual migration. `_migrate_columns` handles the additive `ConversationMessage.thread_id` column on next startup. Existing taste vectors, watch history, memories, recommendations all carry over unchanged.
