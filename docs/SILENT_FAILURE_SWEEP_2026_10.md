# Repository Truth & Silent-Failure Sweep — 2026-10-15

Scope: `src/` and `frontend/` at `df4bfc9`. The sweep has two parts.

- **Part 1** compares every audit written so far against the live code:
  `docs/security/appsec-audit-2026-10-10.md`, `docs/SQLITE_ASYNC_CONCURRENCY_AUDIT.md`,
  `docs/POST_REFACTOR_INTEGRATION_AUDIT.md`, plus the UI/UX, Docs, LLM, Vector/Memory,
  Frontend/SSE and API/OS passes.
- **Part 2** reads every `try/except` in `src/` and every `catch` in `frontend/js/`, looking for
  failures that pass silently as "no data", guards that fail open, and state left half-changed.

Method: reading the code, plus a reproduction for each High finding (scratch venv with the pinned
requirements). The UI/UX, Docs, LLM, Vector/Memory, Frontend/SSE and API/OS passes left no report
in `docs/`, only their commits (`0d69fcf`, `7c6ab55`, `e9e000d`/`f8d2649`/`b068748`/`c299bb8`,
`3a24ba5`/`c61f007`/`879ea9f`, `8bac9e3`, `2d5042a`/`25f8dee`). For those passes, "live" is
checked against the commit messages and the code, not against a findings table.

Size of the problem: `src/` has **766 `except Exception` handlers**. **283** of them only log at
DEBUG and **89** just `pass`. There are no bare `except:` blocks. Most of these handlers are
fine (best-effort enrichment), so the work is finding the ones that guard something.

Regression suite for the fixes applied here: `tests/test_silent_failures_2026_10.py` (39
checks). 32 of them fail on `df4bfc9`; the 7 that pass there assert that LAN, localhost and
host-name URLs stay allowed. Three existing pins changed with the contracts they pinned:

- `test_watch_status.test_music_listening_stats_exception` asserted that a DB failure returns
  None, which is the fail-open case this sweep removes.
- `test_deletion_run_lock` and `test_security_angle2` counted literal
  `set_state("…", "0")` calls in `main.py`. They now check the per-key boot loop.

Battery: 158/158 suites green before, 159/159 after.

---

## 1. Ground Truth Matrix

| Area | Live in `src/` (confirmed) | Missing / report-only | Partially applied / needs follow-up |
| :--- | :--- | :--- | :--- |
| **AppSec (2026-10-10)** | **Fix A** `auth._is_local_browser` (`auth.py:162`): loopback peer, loopback Host, no proxy headers, loopback-or-absent Origin. **Fix B** `endpoint_policy.secret_for_target` (`:85`), wired into `setup.py:103` and `library.py:200`. **Fix C** `endpoint_policy.validate_service_url` as pydantic validators (`setup.py:78/153/180/229/268`, `library.py:51/59`), with classified errors (`setup_wizard._test_failed`, `library._public_test_error`). The report's `url_policy.py` was merged into `endpoint_policy.py`. **Fix D** `recommendations._require_media_id` (`:2622`) on the arr DELETE (`:2734`) and the Plex artist DELETE (`:2640`). **PIN removed**: no endpoint, schema, model or UI; `connection._drop_retired_tables` drops `user_pin_hashes` with `secure_delete`. `/library/profiles` is admin-only. The image proxy is raster-only with `CSP: sandbox`. | **Spotify upload bounds**: `imports.py:44` still does `await up.read()` before the 200 MB check, and the number of files is unbounded. **Error-text items**: `auth.py:92` returns the PyJWT exception text, `auth.py:363` returns the plex.tv body, and `setup_wizard.py:258/279/294` return `str(e)`. **DNS resolution in Fix C** (the report says so itself): a host name that resolves to 169.254/16 still passes. No `TrustedHost` / `ALLOWED_HOSTS`. | **Fix C bypass** (fixed here): `[::ffff:169.254.169.254]`, `2852039166`, `0xa9fea9fe`, `169.254.169.254.`, `169.254.43518` and `0` all passed. |
| **UI/UX** (`0d69fcf`) | `--text3` (5.3–6.4:1) and `--danger-fg`/`--danger-fill` (`app.css:71/75/76`), used 26× and 16×. `prefers-reduced-motion` (`app.css:688-695`, plus the JS shutdown farewell at `curation.js:159`). Sidebar grouped Discover / Library / Curate / System with `<a href="#view">`, a `hashchange` listener (`app.js:65`), `routeFromHash` (`nav.js:62`), `aria-current`, focus on the view's h1. "Why?" disclosure from `score_factors` on deletion cards (`deletions.js:42-72`): a real `<button>` with `aria-expanded`. | "Why?" on **recommendation** cards (no `score_factors` for recs). Tabs inside a view (deletion category, KB tab, arr tab, rec lane) are not in the hash. Modals neither trap nor return focus; `#messages` has no `aria-live`. | **Regression in `0d69fcf` (fixed here)**: `--amber-line`, `--success-line`, `--info-dim` and `--info-line` were defined as `var(<itself>)`, which is invalid. Every amber/success/info badge border, `.banner.warn/.ok`, `.card.is-stagnant`, `.panel-item.live`, `.btn-ghost-amber`, `.discuss-banner`, the poster tint and the glow hover had broken colours. Still open: `kb.js:413` `scrollIntoView({behavior:'smooth'})` ignores reduced motion. "Why?" has no Escape. The `household_floor` guard is only a `title=` tooltip, and no "Why?" button renders when `factors` is empty. `--danger-fill` is defined but never used. `.thinking-status-log` is about 2.3:1. |
| **Docs** (`7c6ab55`, `7d70810`) | README/USAGE first-start, troubleshooting, small-GPU settings; CONTRIBUTING dev setup and lock workflow. | — | Stale comments: `main.py:227-229` and `sync_guard.py:31-33` say Syncthing is re-enabled at shutdown, but `disable()` is now a no-op (`sync_guard.py:186-200`). `taste_engine.py:1444` still says "embedding stored separately when PIN available". |
| **LLM pipeline** (`e9e000d`, `f8d2649`, `b068748`, `c299bb8`) | Judge fails closed on unreadable watch data (`pillars._read_db_facts` → `ow_unknown` → the deferral at `recommendations_engine.py:~2279`). Schema-checked verdicts with one repair turn. Prompts fit the window. The run stops on a game or two outages. | — | `pillars.build_evidence` applies "a failed lookup is UNKNOWN, never empty" (`:352`) only to the SQL watch facts. **Music listening stats** bypassed it (fixed here). Still DEBUG-only and fail-open: verified data (`:619`, where `significance_unchecked` is never set on an exception), owner feedback (`:381/723`), considerations (`:755`), principles (`:771`). |
| **SQLite / Concurrency** (2026-10-11 + 2026-10-15) | `sqlite_autoincrement` and model-derived rebuild; `music_matcher` chunked `write_session`; `force_set_state` path; shutdown drain → writer → `wal_checkpoint(TRUNCATE)`; `runner.run_read/run_write`; per-thread `MetadataCache`; phase A off the loop; atomic `set_state` upsert and `set_states_if_changed`; game watcher off the loop; `_migrate_columns` in one `BEGIN IMMEDIATE` with `user_version`; `update_db.py` backup; `get_db_session` rolls back on `BaseException`. | **Route handlers**: 191 `async def` handlers, 0 sync; only 3 `run_read/run_write` uses in `src/routers/`. **Engine**: `busy_timeout=60000`, `pool_timeout=60`, `pool_size=5`, `pool_pre_ping`, no `foreign_keys=ON` (`connection.py:18-55`). **Leases** for `*_running` flags (boot still clears them unconditionally). **`plex_sync._write_entries`** is still one transaction (`plex_sync.py:802`). **Re-attribution** holds a session across `await _fetch_account_to_user_map` (`plex_sync.py:1315-1319`). **VACUUM** is not gated on idle queues or freelist ratio (`scheduler.py:1150`). | Taste-rebuild embedding phase: up to 3 point lookups per entry still on the loop (documented residual). `set_state` "still locked" logs a warning and returns normally, so `release_state_lock` can report success while the lock stays at "1" (`app_state.py:67-69`). |
| **Vector / Memory math** (`3a24ba5`, `c61f007`, `879ea9f`) | Non-finite filtering, cold-start floor, contradictions supersede, relevance-honest retrieval, migration guard. The original `DetachedInstanceError` fix (cluster hint read inside the session). | — | The cluster block kept its `except Exception: pass` (fixed here). `score_arr_items._vector_score` blanket `except` (the same class as Pass 74) (fixed here). Still open: `embed_service.py:78` caches the default profile after a failed read; `chromadb_wrapper.query` (`:389`) swallows dimension-mismatch errors as "no neighbours"; `semantic_search._vec_memo` is keyed by text only and `_dot` uses `zip`, which truncates; `corpus_repair.py:85-94` deletes before re-adding; `episodic_memory._cosine_similarity` returns 0.0 on a dimension mismatch. |
| **Frontend / SSE** (`8bac9e3`, `0840290`) | Chat keepalive and abort, idle limit, one EventSource opener, history repaint, the reply saved with its turn. | **No central 401 handling** in `api.js` (no sign-out on "Session revoked" or "Account deactivated"). | `activity.js:42-50`: the task stream returns silently on 401 and retries every 3 s forever with no backoff. Bulk-delete poll (fixed here). Setup-status fallback (fixed here). See §2 for the rest. |
| **API / OS lifecycle** (`2d5042a`, `25f8dee`, `6ecbef4`, `ed516fe`) | Model-update notice and test, cleanup after a failed or cut-off test, VRAM measured per context. | — | `model_updates.decide("update")` (`:643-649`) ignores `build_ollama_models` results and marks the update current anyway. The pin `/api/copy` status is unchecked (`:449/453`). The disk-room guard fails open (`:187`). `setup_wizard` `icacls` runs with `check=False` (`:313/322`). The tray autostart return code is unchecked (`tray_app.py:203`). The nvidia-smi timeout or `[N/A]` reads as "no GPU", with no log (`process_monitor.py:142-151`). The game watcher and boot game seeding fail open (fixed here). |

---

## 2. Silent-Failure & Swallowed-Exception Audit Table

"Fixed" means fixed on this branch, with a check in `tests/test_silent_failures_2026_10.py`.
Line numbers are from `df4bfc9`.

| File : Line | Caught Exception / Pattern | What It Actually Masks (Failure Mode) | Severity | Recommended Fix |
| :--- | :--- | :--- | :--- | :--- |
| `services/watch_status.py:58` → `pillars.py:520-532` | `except Exception` → DEBUG, `return None` | **Deletion judge fails open for music.** None means "never played", so a failed read made the evidence say "NO recorded plays…", which the code calls the strongest CUT signal. It also **reset** `owner_watch_unknown` to False, so the deferral gate never fired. Reachable on any `database is locked` during a run (350k rows). | **High** | **Fixed.** The function raises. `build_evidence` sets `owner_watch_unknown=True` and an UNKNOWN line, so the title is deferred. The chat caller says "UNKNOWN". |
| `services/episodic_memory.py:1141` | `except Exception` → DEBUG around ORM reads after `with get_db_session()` closed | **A live `DetachedInstanceError`, the same class as the taste-cluster bug.** It fired on every `proactive_message:*` and `deletion_proposal:*` thread. No thread ever got its anchor line, and `analyze_recommendation_feedback` (the only path for rec follow-up verdicts into taste) **never ran**. | **High** | **Fixed.** Column reads inside the session; WARNING. |
| `services/recommendations_engine.py:1382 / 1414 / 1459 / 1511` (phase A) | `except Exception` → DEBUG (WARNING for ratings), set left empty | **Deletion vetoes fail open**: keep-cooldown (re-pitches titles the owner just kept), curator stances, the 90-day recent-watch veto, and the ≥4★ artist hard-protect. An empty set reads as "nothing protected". | **High** | **Fixed.** Each failure is recorded. The category stops with a run-banner reason ("could not read keep-cooldown — nothing proposed…"). |
| `recommendations_engine.py:1855` | `except Exception` → DEBUG | The listening-depth play map is the −30-point play protection. When it failed, heavily played artists ranked as never played. | **High** | **Fixed.** The music run stops with a reason. |
| `recommendations_engine.py:2016` | `except Exception` → DEBUG, `cons = []` | The learned keep-considerations (up to −45) vanished per candidate, so the judge saw the title unprotected. Realistic whenever the embed model or Chroma is down. | **High** | **Fixed.** The candidate is deferred; if every lookup fails, the run stops with a reason. |
| `recommendations_engine.py:2901` (`score_arr_items._vector_score`) | `except Exception: pass` | The same hidden failure as Pass 74, which the comment directly above describes. A centroid/embedding dimension mismatch (embed model change) made `np.dot` raise for every item, and all ranking silently became genre-only. | **High** | **Fixed.** Explicit shape check, a counter, one WARNING. No blanket except. |
| `recommendations_engine.py:871` (cluster hint) | `except Exception: pass` (left in place by the original fix) | Any malformed cluster entry (`int(None*100)`, a non-string title) or a JSON error dropped the cluster hint **and** `disliked_titles` (discovery exclusions), with no log. | **High** | **Fixed.** Only blob-parse errors are expected (WARNING); the formatting is defensive; anything else logs with a traceback. |
| `recommendations_engine.py:1077` | `except Exception` → DEBUG around `json.loads` **and** `partition()` plus the rec loop | A bug in `library_memory.partition` (the hard owned/seen/deleted guarantee) emptied every discovery lane, and it looked like "the model returned junk". | **High** | **Fixed.** Only the `ValueError` from `json.loads` is caught (WARNING with category and length); the rest propagates. |
| `services/scheduler.py:180` (`job_game_watcher`) | Whole tick in `except Exception` → DEBUG | The VRAM eviction only ran after `lane_status()` and the DB write succeeded. A failing UI-only lane probe or a locked DB **switched game mode off**, silently, every 30 s. | **High** | **Fixed.** Probe, eviction and flag write are isolated, and the eviction runs first. WARNING. |
| `services/scheduler.py:123` (`_gaming`) + `scheduler.py:338`, `main.py:251` | `except Exception` → `False` / pass / DEBUG | **Fails open:** an unreadable `game_active` meant "no game", so ARR sync, proactive messages, recs and the custodian could load the LLM mid-game. | **High** | **Fixed.** An unreadable flag or failed probe counts as a game (WARNING). The startup checks skip their LLM work. |
| `main.py:90` (boot flag reset) | `except Exception: pass` around 5 resets plus the game seed | The only recovery for locks left by a crash. The first failure skipped every later reset, so `deletion_run` / `plex_sync_running` stayed at "1" until the next clean restart. | **High** | **Fixed.** Each key on its own, read back, ERROR when stuck. A failed game probe seeds "game". |
| `main.py:104` | `except Exception: pass` | Interrupted `deleting` proposals stayed invisible: exactly what the block exists to prevent. | Med | **Fixed** (ERROR). |
| `services/endpoint_policy.py:53` | `except ValueError: ip = None` | **The SSRF guard fails open** for non-canonical IP spellings: IPv4-mapped IPv6, decimal, hex and octal forms, a trailing dot, and `0` all reached 169.254.169.254 or 0.0.0.0. Admin or setup-code only. | Med | **Fixed.** All-numeric hosts must be canonical dotted-quad; `ipv4_mapped` is unwrapped; the trailing dot is stripped before the IP checks. |
| `services/discogs_offline.py:276/271` | `except zlib.error` → `stopped`, then the rebuild continues | A game start or corrupt data mid-stream replaced the complete styles DB with a partial one. The fresh mtime then held it for 30 days, with no `"error"`, so the custodian stamped the job done. | **High** | **Fixed.** A stop returns `{"error": …}` before touching the DB. |
| `services/music_matcher.py:383-388` | (no except: the `TRANSIENT` sentinel is falsy) | A MusicBrainz 503/429 burst was recorded as "musicbrainz: no match", parking every artist in the batch for 14 → 30 → 90 days. | **High** | **Fixed.** `profile is TRANSIENT` → counted as failed, no miss recorded. |
| `services/music_matcher.py:786` | `except Exception` → `no_data_keys.add` (DEBUG) | A Last.fm timeout or 5xx, and Last.fm's HTTP-200 `{"error":…}` rate-limit body, were written as `genres=""`, which takes the plays out of the retry pool permanently. | **High** | Not applied. Treat non-200, `error` ≠ 6, and `HTTPError` as transient (leave NULL), and stop after 5 in a row. |
| `services/subtitle_signals.py:780/887` | `except HTTPStatusError` → `""` for any status below 500 | An OpenSubtitles 406 (daily quota) or 401/403 (expired token), and a Plex 401, are stamped "no subtitles" permanently. The local daily budget (400) is above the free-tier quota. | **High** | Not applied. Return `""` only for 404/410; 401/403/406 mark the day spent and return `None` (transient). |
| `services/media_enricher.py:4094` (`process_and_save`) | `except Exception` → DEBUG after the cache write | Two-store half-write: the profile is cached and the item marked enriched, but the Chroma upsert failed, so the item is **never** embedded (the "already enriched" exit). | **High** | Not applied. Mark `vector_pending` and re-embed on the next pass; log with `logger.exception`. |
| `reception.py:522/604/714`, `media_enricher.py:964/1551`, `wikidata.py:252`, `listenbrainz.py:262`, `archive_backfill.py:305` | Walker top-ups: `except Exception` → DEBUG, "transient, try later" | This is exactly how the reception 2-vs-4 unpacking bug hid for months (`reception.py:337-343`). The walkers take `items[:limit]` in a fixed order, so failing rows come back to the front every pass. | **High** | Not applied. Separate transient errors (`HTTPError`, timeouts) from crashes; `logger.exception` crashes; stop after 5 consecutive; add a cursor or attempt stamp. |
| `services/pillars.py:619` (+ `381/723/755/771`) | `except Exception` → DEBUG in `build_evidence` | If `ensure_verified_data` raises, `significance_unchecked` is never set, so the thin-evidence, misfiled and unchecked-significance gates are skipped and the judge sees no acclaim (biased toward CUT). Owner feedback (priority 0), considerations and principles silently drop out of the judge prompt. | **High** | Not applied. On exception set `flags["significance_unchecked"]=True` (deferred); add `owner_signal_unknown` for the others and defer on it in the loop. |
| `services/taste_engine.py:1144` | `gather(return_exceptions=True)` → `continue`, no log | A deterministic `embed_entry` bug produces 0 embeddings, and the taste vector freezes at its old value. The only trace is "Got 0/N embeddings" at INFO. | **High** | Not applied. Count failures, warn with the first 3 tracebacks, and fail the task above 20 %. |
| `frontend/js/deletions.js:592-604` (`bulkDelete`) | `setInterval` poll with `catch {}` | On a 401/500 or network loss the "Deleting N…" button never reset and no outcome came, **on the irreversible action**. A missing task (server restarted) toasted "finished". | **High** | **Fixed.** A 401 or 5 misses stop the poll, reset the button and show a sticky "outcome unknown"; a missing task says unknown. |
| `frontend/js/app.js:39` | `.catch(()=>({first_run:true}))` | Any 500 or restart at page load showed a configured install the Setup wizard, whose Finish rewrites the config. | **High** | **Fixed.** Toast "Can't reach Curatarr", continue to sign-in. |
| `frontend/js/api.js:37` | No 401 handling (throws only) | "Session revoked" / "Account deactivated" mid-session: every view shows a Retry that can never work, and every poller below fails forever. | **High** | Not applied (patch in §3). |
| `frontend/js/activity.js:42-50` | Task stream `return` on 401; fixed 3 s retry | Activity freezes on "Connecting…" or stale Running rows with live Cancel buttons. The badge, the Deletions pulse and the KB live section freeze. | **High** | Not applied. On 401 dispatch unauthorized; on 5xx back off up to 30 s and show "stream down" after 3. |
| `recommendations.py:1852/1879` (bulk loop) | Per-item `except Exception` after a failed `db.commit()` | Half-mutated: after an arr DELETE succeeds, a failed commit leaves the row "deleting" and the session in `PendingRollbackError`, so **every later item in the run fails**. | Med | `db.rollback()` in the handler, then a conditional `UPDATE … WHERE status='deleting'`. |
| `recommendations.py:2664` (`_plex_delete_artist`) | `except Exception` → `False` | An exception in the confirmation GET **after** a 200/204 DELETE: the files are gone but the proposal shows "error". `chk.status_code == 200` treats a 500/401 confirmation as gone. | Med | After a successful DELETE return `None` (limbo) on exception; accept only 404 as gone. |
| `services/soulsync_client.py:131` → `recommendations_engine.py:1560` | `_get` DEBUG → `None` → "follows nobody" | The SoulSync follow veto fails open for the whole music run (documented, but only visible at DEBUG). | Med | Fall back to the cached follow list, or skip music with a reason. |
| `services/library_memory.py:114/134/188`, `watch_status.py:115` | DEBUG → empty seen/owned/removed sets or `{}` | Silently disables the discovery hard guarantee; "NOT watched" appears in deletion chat and legacy pitches. | Med | WARNING with traceback; let `partition` callers see a failure. |
| `services/plex_playlists.py:88/195/228/450`, `plex_collections.py:112/128` | `except Exception` → False/None | With Plex down, every item looks stale. The push returns `[]` and is stamped done for 168 h; collections are deleted and not recreated. | Med | Raise on transient Plex errors (the documented contract); return False when `created < len(designs)`. |
| `services/library_sorting.py:108/286` | TMDB failure **or** non-200 → `set_state(ck,"#0")` with no TTL | "No origin / not animated" cached permanently, which can propose moving anime to the TV root; `ok: True` after a half-applied Sonarr move. | Med | Cache only on 200; report `ok:"partial"`. |
| `services/editions.py:147` | AniDB failure → `_NO_FLAGS` and stamps `checked_at` | Overwrites true uncensored flags; rows vanish from the upgrade list for 7 days. | Med | Keep the previous flags; don't stamp on failure. |
| `services/embed_service.py:78` | DEBUG → default profile **cached for the process** | Latent today (default = live V2). After the next migration or a rollback: wrong model and collection everywhere. | Med | Don't cache on a failed read; WARNING. |
| `vector_store/chromadb_wrapper.py:389/418/273/587` | DEBUG → empty result | Dimension-mismatch and bad-`where` errors read as "no neighbours"; a failed collection switch leaves the wrapper on the old collection. | Med | Re-raise or WARN on `InvalidArgumentError`/`ValueError`. |
| `services/semantic_search.py:186/443` | DEBUG → `[]`; memo keyed by text | A bug in `watched_lookup`/`short_size_tag` empties chat's library context; old-model vectors are mixed after a profile flip. | Med | Narrow the try; key the memo by `(model, text)`. |
| `routers/chat.py:716/788/3735` | DEBUG / fail-open probe / `None` flush result | The flip-flop guard fails open; "keep this" protection detection is silently off on a bug; `clear_chat_history` deletes a thread whose memory flush failed. | Med | WARN; flip-flop failure = treat as unsettled; return `ok` from the flush and refuse the delete on False. |
| `services/corpus_repair.py:94` | Half-mutated: delete, then `add` raises | The doc is gone, counted as "left untouched", and never re-enriched. | Med | `upsert_documents`. |
| `services/curator_principles.py:368` | Novelty check → `{}` (DEBUG) | Every candidate becomes NEW: duplicates and contradictions are stored and ring the bell (High if `PRINCIPLE_AUTO_ACTIVATE`). | Med | Fail closed: skip capture this run. |
| `services/llm_priority.py:152` | `except Exception: pass` → `[]` | `/api/ps` failure → "Confirmed evicted" falsely, reopening the VRAM race. | Med | Return `None` for unknown; WARN. |
| `services/process_monitor.py:142-151/231/335` | Timeout, `[N/A]` or a DB error read as "no GPU" / "no games" | The GPU-pressure gate is off for 600 s per failure with no log; classified games go undetected. | Med | WARN; treat a timeout as pressure. |
| `services/app_state.py:67-69` | "still locked" → WARNING, normal return | `release_state_lock` reports success while the lock stays "1" (half-state). | Med | Return a bool or raise; callers in `finally` escalate. |
| `services/model_updates.py:643-649/449/187` | Results and status codes ignored; disk guard `pass` | A failed rebake is shown as "updated"; rollback restores nothing; a 20 GB pull is allowed with 11 GB free. | Med | Check bake results and `r.status_code`; keep the pin on failure; fail closed on the disk check. |
| `services/setup_wizard.py:313/322` | chmod/icacls DEBUG, `check=False` | `.env` with every token can stay readable by other local Windows accounts. | Med | Check the return code; WARN. |
| `sync_guard.py:94` + `main.py:37` | Unparsable Syncthing `config.xml` → DEBUG → "nothing to do" | The live DB is not excluded from Syncthing (fails open). | Med | WARN and treat as "inside a synced folder". |
| `routers/enrichment.py:2761/873`, `media_enricher.py:2321/1849/2780` | Pre-filter `pass` / pin lookup DEBUG / merge DEBUG / parsing inside a fetch try | Invalidation silently off; the owner's "Fix match" pin ignored; the adult-genre filter bypassed; a parse bug pauses Jikan for an hour as an "outage". | Med | WARN with traceback; treat a pin-lookup failure as transient; move parsing out of the try. |
| `anime_mapping.py:114/146`, `anime_offline.py:103` | Parse error → empty mapping saved after `DROP` | The cross-reference is wiped and cached empty until restart. | Med | Build into a temp table/file and swap; never save an empty parse. |
| `frontend: auth.js:115/122, notifications.js:20/26/78, recs.js:169/211/230, libraries.js:68, chat.js:518, setup.js:522, history.js:93, kb.js:288` | `catch` → `{}` / `[]` / `{configured:false}` | Boot errors send admins into onboarding; the bell freezes and hides a pending model decision; "Regenerating…" stays forever; "No Plex libraries" is cached for the session; discussion history shows empty. | Med | Catch to `{_err}` and render `_errHtml`; count misses; stop on 401. |
| Low (≈30 sites), e.g. `data_custodian.py:100` (`_gaming`→False), `media_enricher.py:1492/2533/2553`, `subtitle_signals.py:674/682`, `spotify_client.py:69`, `music_matcher.py:68`, `discogs_offline.py:215`, `scheduler.py:717/1448/1550`, `main.py:57/63/119/134/172`, `connection.py:424/537/547`, `chat.py:2002/3491/3782` | DEBUG-only optional evidence, budget or stop-flag reads defaulting to "go", cosmetic counts, `str(e)` to the client | Budgets (OMDb, OpenSubtitles, Spotify ban) and stop flags read as "0" or "not stopped" on a DB error. None of them destructive. | Low | Raise to WARNING when touched. Budget reads should fail closed ("spent"). |

**Guards checked and fail-closed (keep these):** JWT decode, subject, token version and
active-account checks; `require_admin`; the setup-code gate; `_is_local_browser`;
`same_origin`/`secret_for_target`; image-proxy IP/DNS; `acquire_state_lock`; ProtectedMedia
loading (not wrapped, so a failure aborts the scan); `_blocked_reason`;
`_lidarr_freshness_guard`; `_probe_arr`; `_require_media_id`; `pillars._read_db_facts`
UNKNOWN flags; `adjudicate` → EVALUATE; GPU-slot release on cancel; extraction cursor that
advances only on success.

---

## 3. Prioritised Cleanup Plan

### Applied on this branch

1. The deletion judge and scorer fail closed: music listening stats, the four phase-A vetoes,
   the listening-depth map and keep-considerations.
2. The second live `DetachedInstanceError` (`episodic_memory` thread anchor + rec-feedback bridge).
3. Game-mode enforcement: watcher isolation, `_gaming()` / startup probes fail closed, boot flag
   reset per key.
4. `validate_service_url` numeric-spelling and IPv4-mapped bypasses.
5. Discogs partial-stream rebuild; MusicBrainz transient-as-miss.
6. Recommendation-side swallows: cluster hint, `_vector_score`, discovery `partition`.
7. CSS self-referential tokens; bulk-delete poll; setup-status first-run fallback.

### Land next (unapplied, in order)

1. **Central 401 handling** (`api.js`) plus the task stream (`activity.js`):

   ```js
   // api.js, after the !r.ok check
   if (r.status === 401 && !path.startsWith('/api/setup/') && !path.startsWith('/api/auth/plex/'))
     window.dispatchEvent(new CustomEvent('curatarr:unauthorized', {detail: _errMsg(err)}));
   // app.js, once
   window.addEventListener('curatarr:unauthorized', () => {
     stopTaskStream(); clearInterval(state._notifPollTimer);
     state.token = ''; localStorage.removeItem('curatarr_token');
     showLogin(); toast('Your session ended — sign in again', 'danger', {sticky: true});
   }, {once: true});
   ```

2. **`pillars.build_evidence` UNKNOWN discipline** for verified data, feedback, considerations
   and principles:

   ```python
   except Exception as e:
       logger.warning("[pillars] verified data failed for %r: %s", title, e, exc_info=True)
       flags["significance_unchecked"] = True           # the deletion loop defers
   # feedback / considerations / principles:
   except Exception as e:
       logger.warning("[pillars] %s failed for %r: %s", what, title, e, exc_info=True)
       flags["owner_signal_unknown"] = True
   # recommendations_engine deferral gate:
   if _fl.get("household_unknown") or _fl.get("owner_watch_unknown") or _fl.get("owner_signal_unknown"):
   ```

3. **Transient vs. definitive** in the enrichers (one pattern, five call sites: Last.fm
   `music_matcher:786`, OpenSubtitles `subtitle_signals:780`, Plex subtitles `:887`, walker
   top-ups, `process_and_save` vector write):

   ```python
   except (httpx.HTTPError, asyncio.TimeoutError, TransientSourceError):
       return TRANSIENT                       # not stamped; retried
   except Exception:
       logger.exception("[%s] crashed on %r", source, title)   # a bug, not an outage
       crashes += 1
       if crashes >= 5:
           raise
       return TRANSIENT
   ```

4. **Bulk-delete commit failure** (`recommendations.py:1879`): `db.rollback()` and a conditional
   status write, so one failed commit cannot fail the rest of the run.
5. **AppSec leftovers**: bounded `spotify_upload` reads (`await up.read(cap + 1)`, at most 20
   files); generic messages at `auth.py:92/363`; resolve names in `validate_service_url` with
   the `image_proxy._host_resolves_public` predicate (private allowed, link-local refused).
6. **Concurrency backlog** (unchanged since 2026-10-11): leases for `*_running`; commits every
   1–2k rows in `plex_sync._write_entries`; fetch the account map before opening the
   re-attribution session; gate VACUUM on idle queues and freelist > 20 %; then move route
   handlers onto `run_read`/`run_write` and lower `busy_timeout`.
7. **Lint rule** so this class of bug can't come back quietly: forbid `except Exception:` whose
   body is only `pass`, or only a `logger.debug`, unless the line carries
   `# noqa: BLE001 - <reason>`. A 40-line AST check in `tests/` fits the "no pytest" convention;
   start it as a ratchet (fail only on new sites) since 372 exist today.
