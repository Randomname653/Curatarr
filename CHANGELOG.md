# Changelog

Condensed release history, newest first.

---

## 2026-08-31 — The record and the work

- **A documentary is not condemned for wearing another film's plot.**
  Sonarr carried `tmdbId 4054` for *Museum of Life*, a BBC nature
  documentary; the correct entry is `40545` — one digit longer. Every
  lookup therefore returned a 1999 Japanese melodrama, its plot was
  cached under the documentary's own keys, and the deletion judge —
  which correctly noticed the plot contradicted the "Documentary"
  genre — proposed deleting the documentary *because* its metadata was
  wrong. Four guards had to fail together, and all four are fixed:
  nothing outranked the arr's mistyped id (TMDB's own tvdb→tmdb index
  is now the authority for series — measured across 2,960 series it
  disagreed 11 times and was right all 11, five of them literal digit
  truncations; an owner's pinned match still outranks everything, and a
  TMDB outage never re-points an entry); the monthly refresher
  re-fetched from the very blob it was refreshing, so a wrong match
  renewed its own TTL forever (it anchors on the live arr record now);
  the judge's path called the enricher without a year, starving the
  wrong-entity delta-check that had existed all along; and no law said
  a misfiled record cannot be a deletion argument — now one does, and a
  gate skips such candidates before the judge ever sees them (title
  *and* year must diverge: on this library that flagged exactly the 8
  genuinely wrong profiles and none of the ~50 harmless
  romanisations). The owner's "Fix match" pin also now holds on the
  live-enrichment path, where it used to silently revert. The 8
  poisoned profiles were purged and re-enrich under the new authority.

- **The last two bot deliveries, sorted.** The dead
  `_running_process_names` helper is gone and the GPU/VRAM picker got
  its label association; the proposed switch away from psutil's
  batched attrs form was declined — on Windows a per-process accessor
  opens a handle per process, which is the opposite of the optimization
  it claimed to be.

- **The security scanner no longer congratulates itself on scanning
  nothing.** An uploaded LLM-security-scan workflow template ran its
  first full sweep — 63 minutes of GPT-4o over 182 files — and filed
  zero issues while reporting "Congratulations! No security
  vulnerabilities were detected." Every single response had been
  discarded: the model wraps its JSON in a markdown fence despite the
  prompt, the parser choked on the backtick, and the error handler
  dumped each file's findings into the CI log and called the file
  clean. The template also executed the scanner *from the PR
  checkout* (any pull request editing that one file ran arbitrary
  code on the runner), interpolated attacker-nameable filenames
  straight into a shell, and pinned tj-actions/changed-files@v40 by
  mutable tag — the action whose tags were repointed to a
  secret-dumping commit in March 2025. Rebuilt: PR runs scan only the
  changed files with the scanner taken from the trusted base commit,
  filenames travel NUL-delimited so no shell ever parses them, the
  git two-liner replaces the compromised action (closing the
  Dependabot bump as moot), fenced JSON parses, a failed analysis is
  an ERROR in the report instead of a clean bill, and issues are
  filed from the JSON report — deduplicated, capped, and only from
  the trusted weekly sweep. Full-repo scans left the push trigger
  entirely: CodeQL covers pushes for free.

- **The lost hour was salvaged, and the verdict is good news.** The
  discarded findings were reconstructed from the CI log's own error
  dumps: 317 findings across 158 files, 82 of them Critical or High.
  Six triage agents read every one of the 82 against the actual
  source: **zero were real**, four were hardening notes (three of
  them documented design decisions), and 78 were noise — settings
  lookups read as hardcoded credentials, ORM filters read as SQL
  injection, a pydantic default read as a secret, a docstring read as
  a cryptographic flaw. The one change worth making: the Wikidata
  SPARQL query now accepts only canonical `tt`-shaped IMDb ids, since
  ids arrive from the arrs and a quote in a corrupt one would rewrite
  the query and confidently fetch a stranger's facts — the exact
  failure class the entity-authority work exists to prevent.

## 2026-08-28 — No verdict on data we don't have

- **The curator can finally hear the film.** Until now it judged only
  metadata *about* a work and had never seen a line of the work itself —
  while the constitution demanded it separate premise from execution and
  the no-invention rule forbade any execution verdict without evidence. It
  was asked to judge what it was blind to. Deletion candidates now carry a
  DIALOGUE line derived from their subtitle track: words per minute, how
  much of the runtime passes without dialogue, and lexical diversity. The
  measured spread on this library is a factor of three — a contemplative
  drama at 42 words/min against a dialogue piece at 125 — which is exactly
  the distinction the Resonance pillar's litmus had to guess at, having no
  rhythm evidence at all.

  What it deliberately is *not*: raw type-token ratio is mathematically
  length-biased and would rank a long film as less varied by construction,
  so the code uses a moving-average measure and a test proves the
  difference by doubling a text. Sentiment "narrative arcs" are absent
  because the six-shapes result that popularised them was shown to be an
  artifact of the filtering, conceded by the tool's own author. And the
  numbers describe a *subtitle track* — condensed by professional style
  guides, often a translation, timed imprecisely — so the caveat travels
  with the figure every single time, and the law states plainly that
  sparse dialogue is not thin writing: visually-driven cinema is what the
  Resonance pillar exists to protect, not something it may punish.

  Forced tracks (signage only) are detected and refused rather than
  measured, since one would report a talkative film as nearly silent; SDH
  sound cues, lyrics and speaker labels are stripped before counting; and
  CJK scripts are refused outright because a words-per-minute figure
  without word boundaries is an artifact of the writing system. Language
  is read from the text, never the track's tag — this library carries a
  track tagged Hindi containing no Devanagari at all. Where no reachable
  track exists the line is simply absent: silence beats a guess.

- **ASS subtitles, the format anime actually ships in.** The parser read
  SRT and WebVTT, which covers films and western series and almost no
  anime: fansubs are ASS, and ASS is not SRT with different punctuation.
  Its events are comma-separated records whose field ORDER each file
  declares for itself, its timestamps count centiseconds, and a single
  file routinely carries parallel tracks — the dialogue, the translated
  signs, the opening karaoke. Reading the signs track would report a
  talkative episode as nearly silent, exactly the damage a forced track
  does, so styles named for signs, songs, karaoke, credits or typesetting
  are skipped; `Comment:` events are skipped as they are never rendered;
  the ASS line-break escape is treated as the break it is rather than a
  word; and an event that was only a vector drawing no longer counts as
  speech. The field order is read from each file's own `Format:` line,
  because groups do reorder it and a hardcoded index would quietly parse
  a style name as a timestamp.

- **A misconfigured URL must not be mistaken for a verdict.** First
  connection to a real service caught the failure mode this whole
  tri-state exists for: the base URL was missing its path prefix, every
  request came back 404 — and a 404 was read as "this title genuinely
  has no subtitles" and would have been stamped as checked. One typo
  would have marked an entire library subtitle-less, permanently and
  silently. Now a response that is an HTML page rather than subtitle
  data is transient by definition and logs which URL it came from, and a
  bare host gets the default path prefix appended instead of failing.

- **Thai is not a quiet film.** Asking the provider for German returned a
  Thai track — 480 cues carrying 74 whitespace-separated "words", which
  the metric read as an almost silent episode. Thai, like Chinese and
  Japanese, writes without spaces between words, so counting them
  measures the writing system rather than the dialogue. CJK was refused
  from the start; Thai, Lao, Khmer, Burmese and Tibetan were not, and now
  are. A second guard catches whatever the script detector does not know:
  a real dialogue track averages four to eight words per cue, and a
  fraction of a word per cue means the text is not being read as language
  at all.

- **A subtitle longer than its own episode is somebody else's file.** The
  coverage check was one-sided: too FEW cues over the runtime meant a
  signage-only track and was refused, but too MANY was accepted without
  question. A 22-minute episode came back with a subtitle spanning 52
  minutes, every word of it divided by the episode's runtime — 243
  words per minute, nearly double anything else in the library, from a
  file that simply belonged to something else. The stored coverage was
  additionally clamped to 1.0, so the mismatch was invisible even in the
  data. Both fixed: coverage above 1.3 is refused, and the figure is
  recorded as measured.

- **An optional subtitle service of your own — and a bug worth the whole
  integration.** Anime is where dialogue evidence is thinnest: only about
  a quarter of it carries a subtitle file Plex will hand out, and public
  sources have little for the catalogue titles that dominate deletion
  candidates. Operators who run their own subtitle service can now point
  Curatarr at it. Nothing is bundled, no service is named, and none is
  assumed: the module documents an HTTP contract and stays completely
  inert when unset — no connection is opened, and an absent service is
  reported as transient rather than stamped, so a stranger's install can
  never mark its whole library as subtitle-less on the strength of a
  service it was never going to have. Order of preference: the local file
  first (free, instant, matches the cut on disk), then the operator's own
  service, then the public fallback that costs quota.

  The integration paid for itself before it was even connected. The
  service author found that their track-name filter matched `title`
  without a word boundary — which matches *Subtitles*, the commonest name
  a dialogue track has. The same pattern was in our ASS style filter: a
  fansub whose dialogue style is named `Subtitles` would have had every
  line discarded, fallen under the forced-track threshold, and been
  recorded as having no usable subtitles — silently, with no error
  anywhere. Every alternative is word-bounded now, on both sides.

- **And in a discussion, it can read the dialogue itself.** The batch judge
  only ever sees the derived numbers — one feature's dialogue is around
  13,000 tokens against a 16k context — but pressing "look deeper" on a
  single title now also pulls the cleaned dialogue text into the
  conversation, the same trade the Wikipedia deep read already makes. It is
  sampled beginning, middle and end rather than truncated, because a blind
  head-cut hides exactly the late shift in register a discussion tends to
  turn on, and it is labelled for what it is: evidence of register and
  texture, not a transcript of record.

  Where Plex has no downloadable track — which is most series and a quarter
  of the films — OpenSubtitles now fills the gap, matched by the IMDb id
  Plex hands over directly, so there is no title guessing. Quota is the
  entire design constraint: the anonymous tier allows five downloads a day
  and a free login ten to twenty, against up to sixty candidates in one
  scan. A daily budget is therefore checked *before* any connection is
  opened, and exhausting it reports as transient rather than being stamped —
  otherwise a busy day would mark a title as having no subtitles forever.
  The local file always wins when it exists: free, instant, and guaranteed
  to match the cut on disk.

- **A batch of agent-written tests, kept only where they bite.** Thirteen
  bot PRs landed at once; each was reviewed against its brief, run, and
  then mutation-tested — the production function it covers was broken on
  purpose to see whether the test noticed. Seven new suites survived
  (anime mapping, SoulSync album payloads, tasks router, studio and
  director note caches, Spotify backoff, watch status, history router),
  taking the battery from 57 to 64 suites. Two findings were worth more
  than the tests themselves: the watch-status early-exit test passed even
  with its guard removed, because the call then fell through to a real
  database session whose failure the function's own except turned back
  into `None` — a green test quietly hitting the live DB. Its fix had to
  raise a `BaseException`, since the obvious `AssertionError` would have
  been swallowed by that same except. And the playlist test asserted
  nothing about the change it shipped with: removing the threaded
  argument left it green at 14/14. Both now fail when they should.

- **Playlist pushes resolve their Plex sections once, not per item.** The
  artist and video key resolvers each opened a database session and
  re-read `LibraryConfig` on every call. The push now resolves it once
  and hands it down; passing nothing keeps the old behaviour, so no other
  call site changes. Deliberately not a TTL cache — that config is edited
  during onboarding and in Settings, where a stale window is a real bug.
  Honest scope: every resolve also does an HTTP round-trip to Plex that
  dominates the database read, so this removes redundant work, not
  user-visible latency.

- **Spotify: a ban seen on the retry is remembered too.** The first 429
  read `retry-after` and persisted a long backoff across restarts; the
  retried request's 429 only counted and bailed, so a quota ban that
  surfaced one request later was forgotten on the next start and we went
  back to knocking — which can restart the 24-hour window.

- **A declared intention to watch now ACTS.** "Sounds promising, I
  shall put it on my watchlist" ends a deletion discussion in favour of
  keeping — and the backend now does what the curator announces: the
  post-turn protection scanner recognises watch-declarations as
  keep-directives, protects the title, and adds it to the DISCUSSING
  user's own plex.tv watchlist (their OAuth token, so it appears in
  their Plex apps — per user, never the server owner's). Resolution is
  deliberately cautious: word-multiset title match against Plex
  Discover, expected media type, year as tiebreaker — same-name twins
  without a year are refused, never guessed. The chat LLM still executes
  nothing; a deterministic hook does. En route, an honesty bug died: the
  curator's announced "I am flagging X for a downscale" never reached
  the downscale work list (discussion keeps wrote no verdict and the
  list only showed judge rows). The flag is now decided by the tech
  profile — never by the LLM's prose — written as KEEP_WITH_FLAG on the
  discussion keep, and the work list is verdict-gated so judge-granted
  and discussion-reached flags both surface. The curator's discussion
  rules say exactly what the backend does, and that it may announce
  that and nothing beyond it.

- **The setup wizard learned what fits your GPU.** The Ollama step now
  probes the host GPU on request (nvidia-smi, with a manual VRAM picker
  for remote Ollama boxes) and recommends models from a bench-verified
  catalog instead of a hardcoded default: entries carry honest fit
  verdicts (weights + context headroom — the lesson of a 22 GB model
  starving its KV cache on a 24 GB card), already-installed models are
  marked as zero-download choices, anything else on the server stays
  selectable but labeled untested, and below the verified floor the
  wizard says so instead of pretending. After the bake, a warm-up check
  loads the curator once and reads Ollama's own GPU-residency report — a
  model that silently spills onto the CPU is flagged right there, not on
  the user's first crawling conversation. The catalog is enforced against
  the benchmark CSV by its own test suite, so a future bench run that
  re-roles a model must be reflected deliberately.

- **File size argues the file, never the work.** A Level-2 scan conceded a
  title was a documented landmark AND a genuine taste fit — both pillars
  keep — then closed with "most importantly, the file is a disaster" and
  recommended deletion on bitrate. Root cause was a law conflict we
  shipped ourselves: the pillar framework said a bitrate outlier is a
  downscale note, never a delete reason, while the injected SIZE CONTEXT
  line licensed size as "a fair secondary argument" — and the fresher,
  number-bearing license won. The source line now says what size may
  argue: a downscale of the file on a keep, never deletion of the work,
  never a tiebreaker, and "delete now, re-acquire leaner later" is not a
  path to offer — the downscale flag IS the lean path. The Level-2 rules
  gained the missing spine: a scan whose own findings concede a keep under
  any pillar must move its verdict (re-anchoring on extra-pillar grounds
  like file size or unwatched status is a broken scan), a STAGNANT verdict
  is a hand-off to the owner's judgment rather than a delete verdict to
  defend, and the forbidden-contempt register is banned by shape — fresh
  coinages like "digital landfill" violate it the same as the listed
  phrases.

- **A declaration closes the talk; a question earns an honest answer.**
  First cut of the interest rule treated any spark of curiosity as a keep
  signal — dangerous the other way, as the owner pointed out: every
  question would become a self-fulfilling keep, and the mandatory steelman
  (which is supposed to be strong enough to spark curiosity) would neuter
  the funnel it serves. Calibrated: a DECLARED intention ("I want to watch
  this") is first-party evidence and closes the deletion talk; a QUESTION
  ("is this worth watching?") mandates nothing and may honestly be
  answered "no, the pitch stands" — but the answer binds the answerer:
  a curator that tells the owner a title is worth their time cannot offer
  the delete button in the same breath. Unclear which it was? Ask, one
  sentence, no pressure.

- **Bot harvest + one real PII catch.** Three waiting bot branches
  landed as direct commits: the unused `exe` attribute is gone from the
  process scan (an OS path lookup per process, read by nothing),
  five unlabeled `<select>`s got aria-labels, and one branch pointed at
  actual PII the frozen-directory sweep had skipped — a benchmark comment
  still naming the author's Windows user in the very sentence describing
  the hardcoded path it replaced.

- **The law is one law at every site.** An adversarial consistency sweep
  across the four prompt-law sites found the size-license bug had a
  surviving twin: the legacy pitch prompt (the `PILLARS_ENABLED=false`
  path — the `.env.example` default, so live on every fresh install) still
  told the model to make size "a brief secondary point" in a deletion
  pitch; it now carries the same downscale-only doctrine. Also closed:
  the analytical-integrity rule listed only three valid reversal grounds,
  giving the persona verbatim cover to dismiss the owner's first-party
  word as "emotional language" — first-party evidence is now the fourth
  ground and explicitly overrides the hold-your-position default; the
  neutral three-path close no longer re-lists delete after the owner has
  decided or expressed interest; a keep conceded in a thread binds every
  later turn of it; ambiguous owner signals read as the stronger one; a
  bitrate outlier can no longer demote a hard keep to "gray zone"; and
  the always-on size rule in chat carries the doctrine even outside title
  discussions.

- **Reordered romanised names are the same work.** A drama with a reachable
  13,000-character Wikipedia article scanned as "lean data" because three
  individually sound guards composed into blindness: the entity had no
  English sitelink (true), the franchise article's manga-first lead failed
  the direct path's plausibility gate, and the search returned the article
  under a title with the Japanese surname/given-name order swapped — which
  the order-sensitive equality guard called a different work. The hit guard
  now accepts word-multiset equality (same words, any order) while
  different words and cross-medium disambiguators still reject. The fix
  heals both the significance scan and the deletion discussion's deep-read
  path, which shared the guard; the retrieval version bump re-offers every
  title stamped empty under the old rule.

- **The judge defers titles whose data isn't there yet.** The thin-evidence
  gate caught "no enrichment at all", but partial enrichment dodged it: a
  silently absent significance line read as "no documented stature" and
  condemned titles our own pipeline simply hadn't fetched. The cache's
  tri-state now reaches the judge — significance never successfully checked
  defers the candidate (it re-enters next scan; the pre-judge warm-up
  retries the fetch), while checked-and-definitively-empty remains
  judgeable, because that IS evidence. When a block is built anyway, the
  gap is named as missing data instead of left silent.

- **The fuller plot wins, whoever wrote it.** The verified block preferred
  OMDb's "full" plot by source, so a one-line OMDb entry displaced a rich
  TMDB overview. The longer of the two API texts is used now.

## 2026-08-25 — The benchmarks go public, and the docs stop lying

- **Disagreement got a register, and unknown categories get learned.** A
  discussion showed the previous fix half-landing: the curator now defers
  and offers the downscale, but still scolds — "hoarding", "storage tax",
  "stop romanticizing mediocrity". Prose bearing-rules lose to an
  "uncompromising" persona, so the forbidden register is now named
  verbatim, the curator may disagree exactly once before presenting the
  paths neutrally, and weak attachment ("I kinda like it") counts as
  attachment. Deeper fix: when the owner invokes a retention category the
  constitution lacks — camp, "so bad it's good", curiosity value — the
  correct move is not to deny the category exists (it is recognised
  aesthetics, Sontag onward) but to offer recording it as a learned
  curation principle, which the debate-learning loop was built for. The
  argument becomes the system learning instead of the system winning.

- **The owner's word about their own life is evidence, not testimony.** A
  live discussion exposed the anti-sycophancy spine overcorrecting: the
  owner attested three full watches of a trilogy — first-party Pillar-0
  evidence the server's history could not contain, since the viewings
  predate it — and the curator dismissed it as "sentiment, not curation",
  lectured about scrapbooks, and re-offered the delete button after being
  overruled. The discussion rules now distinguish the two kinds of owner
  testimony: claims about the WORK stay unverified testimony to be named as
  such, but claims about the owner's OWN viewing and attachment are
  first-party evidence that wins a graceful concession — that is the
  constitution working, not sentimentality. Bearing rules ride on every
  discussion turn: no contempt, no re-offered deletion after the owner
  decides, and an overruled bitrate outlier gets one constructive downscale
  offer instead of a parting shot.

- **The pitch can no longer recite the owner back at themselves.** The
  first proposal batch after the taste-as-data change proved the pipeline
  (adaptation credits, ratings and episode counts argued from evidence; no
  confabulated facts across ten pitches) and exposed the last leak: OWNER
  TASTE is stripped from the monologue's inputs, but the judge's governing
  finding rides along "for reasoning only" — and the model paraphrased its
  taste language back as "you consistently demand…", with the no-size rule
  leaking as "footprint on your disk". A prose rule cannot enforce this; a
  shape check can: recitation and size-talk are detected on the output,
  regenerated once with the violation named, and logged if the retry still
  fails. Calibrated against two live batches: the first supplied the five
  leak shapes, the second — run through the check — got recitation down
  from five pitches to one and surfaced the two survivors ("you typically
  seek", "occupying space") that widened the patterns. A third batch then
  routed around the word list entirely ("you consistently reward", "your
  library demands", "your viewing standard") and settled the design: the
  check now matches the SHAPE — a second-person possessive reaching a
  taste-noun, or you/your-library carrying a claim-verb — instead of
  enumerating phrasings, and the retry instruction bans attributing
  anything to the reader in any wording. "In your library", "your
  attention" and "your time" carry neither form and stay legal, as does
  arguing down cited acclaim — the constitution working, never a
  violation.

- **The owner's taste left the constitution.** A Jules security pass
  noticed what every local sweep had missed: the deletion judge's Pillar-0
  text hardcoded the owner's personal taste profile — including verbatim
  personal phrasings — into the public code, which was both a privacy leak
  and a correctness bug, since every other install inherited that taste as
  law. Thought through to the end: taste is per-user DATA, and production
  already injects each user's own profile as the OWNER TASTE evidence line,
  so the constitution is now deliberately taste-blind and defers to that
  line (with an explicit rule that an absent line cannot condemn). Test
  fixtures keep a pointed owner — a fictional one, with every case's
  decisive lever intact rather than the diluted wording the bot proposed;
  the live verdict gate should be re-confirmed once before the next model
  swap.

- **Three more Jules deliveries, harvested mid-flight.** The chat send
  button and input now disable while a message is processing and re-enable
  in a `finally` (no more double-sends, no lockout on error), missing
  aria-labels landed on the *arr settings and bulk-delete inputs, and two
  approved structural refactors arrived exactly as specified — including
  the `_store_taste_blobs` name where the old encryption fiction was
  vetoed. Its bundled dev scaffolding (`run_uvicorn.sh`, a verify script)
  was dropped at the door.

- **The repo is armed for public operation.** Dependabot config (pinned
  pip deps, grouped minor/patch bumps, actions updates), a CodeQL workflow
  that stays dormant until the repo is public (schedule + manual trigger;
  push trigger documented for arming), issue forms that ask for scrubbed
  logs, and a PR template whose checklist is the house rules — battery
  green, behaviour changes carry tests, no personal data, rebase onto the
  rewritten main. All inert until the corresponding switches are flipped
  in the repo settings.

- **Three more Jules branches landed as authored.** The title-match
  scorer's regex and stop-word set are hoisted to module level (~25% faster
  in the nested matching loops), the SQLite files under `data/` get the
  same `0600` treatment as `.env` on POSIX, and the unauthenticated PIN
  rate-limiter is capped at 1,000 buckets with amortised eviction — the
  one genuinely reachable memory-DoS surface, closed. A fourth branch
  duplicated the German-string translation already adopted from its
  predecessor PR and was skipped.

- **The taste-vector "encryption" was removed instead of shipped.** The
  README offered "an opt-in PIN-based AES-GCM path"; in reality two parallel
  implementations existed and neither had a single caller — the table named
  `encrypted_taste_vectors` holds plain JSON. The design cannot work here:
  the key derives from a PIN the user types, but the vector's consumers are
  background jobs that run precisely when nobody is present to type it, and
  the watch history the vector derives from sits in the same database in
  plaintext anyway. Dead cipher code, its config knobs, its dependency and
  its tests are gone; README and SECURITY now say the honest thing — data at
  rest is unencrypted, use disk encryption if the disk is your threat model.

- **Spotify listening history imports through the GUI.** Drop the extended
  streaming history (single files or the whole `my_spotify_data.zip`) into
  the new Setup → Import step or the Admin view, pick whose listening it
  is, done — progress in the Activity view, duplicate-safe, and the basic
  export is refused with an explanation because it lacks the completion
  signal replay counting depends on. The old script survives as a headless
  wrapper over the same engine.
- **The setup wizard catches up with the app.** It now asks for everything
  the app can use: OMDb — which the wizard's request model silently
  DROPPED until now, so a key entered there was never saved — and the
  optional SoulSync neighbour. Model pickers fall back to the benchmarked
  pair instead of two generations of stale recommendations, and a test now
  asserts the schema's three homes (field list, request model, frontend
  form) agree, which is the drift that hid the OMDb bug.
- **The five auto-generated PRs the force-push closed were read and
  adopted** where they held up: keyboard `:focus-visible` styles, `for=`
  attributes on form labels, the rate limiter no longer sleeps while
  holding its lock (a convoy that serialized every burst) and refills on a
  monotonic clock, the genre-absence detector runs in one pass, and the
  last German strings left the UI — a deletion-protection button and the
  game-detection toast my earlier sweep missed.

- **The model benchmarks are in the repo.** Five candidates, 740
  hand-scored pitches, a JSON stress gate and a 33-case chat bench —
  method, verdict and raw data in [docs/BENCHMARKS.md](docs/BENCHMARKS.md),
  with the score CSVs, the August report, the owner spot-check and the
  harness scripts checked in so the reasoning can be audited or rerun
  against your own model. Headline finding worth publishing on its own:
  the pipeline winner and the chat winner were different models, and the
  pipeline's +0.2 quality at 2.4× speed lost to a sycophancy collapse the
  moment the metadata anchor was removed.
- **The docs recommended a model our own benchmark disqualified.** The
  README suggested the curator base that the August run timed out into
  600-second stalls (VRAM starvation at 16k context on 24 GB); the setup
  wizard's defaults were a model generation older still. README, wizard,
  config defaults and ARCHITECTURE now all name the benchmarked production
  pair, and every command, setting and troubleshooting claim in the usage
  guide was verified against the code — one stale claim fixed: the
  Syncthing exclusion is permanent, not per-run.
- **Commit identity moved to GitHub's noreply address**, and the history
  was rewritten once to scrub the personal address from past commits.

## 2026-08-22 — What the judge is given to reason from

- **Community reception was not slow, it was broken.** One exit from the
  reception builder returned two values where every other returned four, so
  the caller raised on every film and show. The exception was logged at debug
  level, which made a total failure look like a slow backfill for months —
  anime left through a different branch and worked, which is exactly why the
  gap read as an ordering problem. A second exit returned three. Both fixed,
  and a test now asserts that every exit from that function agrees.
- **The distiller was reading the wrong article.** Wikipedia states what a
  subject is in its opening sentence; the plausibility check scanned 1,500
  characters past it, far enough to reach a passing mention. So a lookup for
  the 2013 series accepted the page on the Birmingham street gang that
  inspired it, the distiller correctly reported no significance, and that was
  stamped as a permanent verdict. The same held for Fargo and Alien, whose
  disambiguation pages open "usually refers to" and "most commonly refers to"
  while the guard knew only "may refer to". Judged on the lead sentence now,
  with the whole family of disambiguation phrasings caught: Peaky Blinders
  returns its National Television Award, Fargo its seven Oscar nominations,
  Alien its Academy Award. 2,069 titles carried an empty "no significance"
  stamp, and the ones that carried text were no safer — where the wrong
  article had awards, another work's standing was attached to the title.
- **A failed second fetch is no longer a permanent verdict.** The tri-state
  return exists because a transient failure once left a title
  significance-less for good. The search call honoured it; the follow-up
  article fetch did not, and a rate-limited response fell through to
  "definitively nothing". Wikipedia also gained the backoff every other
  rate-limited source here already had — it was the only one that answered a
  "slow down" by moving on.
- **The stamp now covers how the article was chosen, not only what the model
  was asked.** A perfect prompt over the wrong page yields a confident
  nothing, so retrieval rules are part of the version hash and tightening
  them retires the answers they produced. The just-in-time path tests that
  version too; it used to gate on the bare "checked" flag, which is what made
  a wrong verdict permanent there.

- **The privacy section said less than the code does.** "Only ids go out"
  was already inaccurate: enrichment searches TMDB, Wikipedia, Jikan,
  AniList, MusicBrainz and Last.fm *by name*, so those services learn which
  titles and artists a library holds. OMDb alone is queried purely by id.
  Corrected in the README, ARCHITECTURE and SECURITY, and stated the way it
  actually splits: titles go out, behaviour does not — watch history,
  ratings, the taste vector and anything typed stay on the machine. The
  local-LLM promise is unchanged and remains true.
- **Dead cache rows are collected again.** An earlier pass removed the
  expired-row cleanup because nothing called it; the rows kept accumulating,
  13,201 of 92,474 of them, on a table two hot paths scan in full. Every read
  path already filters on expiry, so they were unreachable rather than merely
  stale. A daily custodian tick now prunes them — the lesson taken last time
  was to delete the method, the one available was to call it.

- **The same class, hunted across the codebase.** Four more instances of
  transient-as-permanent, each fixed the same way: studio and director notes
  cached "NONE" for *ten years* when Wikipedia or the summariser merely
  failed to answer (394 poisoned rows purged; they re-resolve on demand);
  Last.fm negative-cached a 429 as "no such artist" for a week; the
  MusicBrainz→Deezer resolver froze a thrown request as "no link" for 60
  days; and the chat-memory extractor advanced its cursor past message
  windows the model never processed, permanently dropping them from
  personalisation. Director names are also matched with romanisation folding
  now — the credits say "Shinbou" where Wikipedia writes "Shinbo", and the
  exact-name guard permanently rejected the very people it was built to
  find.
- **A silent regression, caught three times by its own class.** The
  cache-addressing patch used `cache_id` inside functions that never got the
  parameter: `topup_franchise` (every call a NameError, swallowed at debug
  level) and `build_verified_data`, where the chat's deletion discussion
  surfaced it as a 500. Both fixed; `topup_franchise` also gained the
  transient guard it never had (an AniList rate-limit no longer stamps "no
  franchise graph" permanently). A test now walks the AST of the whole tree
  and asserts every function declares the names it uses.
- **Shutdown frees the GPU instead of abandoning a timer.** Stopping the
  app inside the curator's 10-60s idle window tore the event loop down under
  a sleeping evict task ("Task was destroyed but it is pending!") — and the
  eviction it was about to perform never ran, so the model squatted in VRAM
  until Ollama's own keep_alive expired, on the machine whose GPU the app
  was most likely closed to free. The lifespan teardown now cancels the
  timer and evicts curator and pitcher immediately, best-effort.

- **The setup wizard no longer eats hand-added settings.** Re-running it
  rebuilt `.env` from its template alone, silently destroying every key
  outside it — ten of thirty-three on the reference install, including the
  SoulSync connection. Unknown keys are carried over verbatim into a
  preserved section.

- **Silence is no longer mistaken for emptiness.** A TMDB 429 and "this
  film has no reviews" both arrived as an empty list, so an outage was
  stamped as a permanent "no community data" — 1,148 of 4,570 checked titles
  carried nothing at all, indistinguishably. Every reception fetcher now
  answers three ways (data, definitively nothing, did-not-answer), a source
  that did not answer raises instead of stamping, and a busy condenser counts
  as not answering. Checked-but-empty entries from before the distinction
  are offered exactly once more. OMDb had the inverse bug: "Movie not
  found!" — a definitive answer — was treated as transient, so the title
  stayed pending forever and was re-queried on every walk; found-nothing now
  stamps, quota exhaustion still never does.

- **The article is resolved by identity, and only then by name.** Asked why
  only English Wikipedia is used, the measurement answered a different
  question: of 120 titles stamped "no significance" that carry an IMDb id, 72
  had an English article the name search could not reach — filed under
  disambiguators like "The Fall Guy (2024 film)" — while only 6 had solely a
  Japanese article and 0 only a German one. The bottleneck was never the
  language; it was guessing names. The IMDb id now resolves the exact article
  through its Wikidata sitelink before any name is tried, with the entity's
  claim verified rather than trusted from search — tt0000000 exists on
  Wikidata as someone's placeholder, attached to a real film, so all-zero ids
  join nothing. A sitelinked article needs none of the name guards: identity
  came from the id.

- **Every name of a work gets a Wikipedia turn.** The library files
  "Frieren: Beyond Journey's End" — which is the article's name — while the
  enriched record inside says "Sousou no Frieren", and the search ran on the
  inner name alone. The exact-match guard then rightly refused to bridge two
  different names, so one of the defining anime of the decade was stamped
  "no documented significance"; in a sample of the affected class, Wikipedia
  had the article for 8 of 13. The library's title now rides along as a
  known alias and the direct lookup and the search try each name in turn.
  Recommendation lists (`similar_titles`) are explicitly never aliases —
  those are other works, and using them would attach a neighbour's fame.
  What remains empty after this is mostly honest: a mid-tier seasonal anime
  has no English Wikipedia article, and its standing evidence comes from the
  reception and on-record sources instead.

- **A walker that could not find the row it was meant to write.** Raw cache
  entries are filed under the *library's* title, while the `title` inside them
  is the enriched one — so the row for "Frieren: Beyond Journey's End" is
  titled "Sousou no Frieren", and "Dan Da Dan" holds "DAN DA DAN". Every
  top-up rebuilt its lookup key from that inner title, missed, found nothing
  to write and returned "nothing to do". The walker counted the title as
  visited and moved on, so it stayed outstanding for good and coverage
  plateaued with no error anywhere. Measured: 342 of 400 pending Wikidata
  titles were unreachable, and 30% of pending significance titles. The key a
  row was read from is now carried into the work list and tried first —
  the same lesson as the *arr ids: do not re-derive an identity you are
  already holding.
- **The backfill keeps a couple of titles in flight.** Measured per
  significance title: 0.6s of Wikipedia and 2.0s of model, strictly
  alternating, so the card idled through every fetch and the network through
  every distillation. Overlapping them is worth about 23% — which is also the
  entire ceiling an offline Wikipedia copy could offer, at 100 GB and a
  rewritten retrieval path. Deliberately narrow: Ollama serialises requests
  for one model on one card, so a wider pool would only queue, and would press
  harder on services that asked us to be gentle.

- **Wikidata joins Wikipedia** rather than replacing it. Wikipedia is prose
  and must be distilled; Wikidata is a graph of statements, so the source of
  an adaptation and its named awards are looked up rather than summarised —
  no model, no key, nothing to get wrong. They sit on separate evidence
  lines so it stays obvious which was distilled and which was read.
- **The *arr services already knew the ids we were missing.** A quarter of
  the library had no IMDb id — anime is enriched from AniList and never
  touches TMDB — and two sources key on exactly that id. Sonarr held the id
  for 1,833 of those titles all along, from a call made on every sync.
  Harvesting them lifted id coverage from 76% to 96%.
- **Deciding *which* entry a title refers to is the part worth distrusting.**
  What an *arr says about its own entry is ground truth; matching a cached
  title to that entry by name is not. So an id join comes first and reads no
  title at all, the name is only consulted afterwards, and the year holds a
  veto over it — franchises reuse names across decades. Anime is looked for
  in Radarr as well as Sonarr, because anime films live there while the
  series live in Sonarr. Audited against an independent anidb→tvdb mapping,
  the name step agreed on 1,788 of 1,796 checkable titles; seven of the eight
  disagreements were the mapping pointing at a parent series. The one real
  error was a 2013 film wearing the id of the 1978 series of the same name —
  the year was in both records the whole time, unused. Claims carry the rule
  version that produced them, so tightening the rules offers the old ones for
  re-judgement rather than freezing them.
- **Backfill is offered where the gap is visible.** A fresh install has none
  of this metadata, and the daily tick fills it at a pace meant for a library
  that has been running for months. The Knowledge Base now shows a "Finish
  the backfill" panel with coverage per source and a button each — and it
  removes itself once a source is well covered, leaving the remainder to the
  ordinary ticks. Titles that can never gain a source (no IMDb id, a quarter
  of the library) count as settled rather than holding the offer open for
  good. `scripts/facts_speedrunner.py` does the same from a terminal, with
  `--skip-significance` for a run that never touches the GPU.

- **The source of an adaptation is now fetched.** Only the screenwriter was
  ever read, so an adaptation of a celebrated novel looked like an anonymous
  genre piece — the metadata said "based on a novel" without ever saying
  whose. The novelist, playwright or original author is pulled from its own
  credit and named separately from the adapter.
- **Cast lists are no longer mistaken for cultural significance.** The
  distiller occasionally returned a verbatim Wikipedia cast section, which
  left the archive pillar reading a character list as standing. A prose rule
  forbidding it already existed; a shape check now enforces it.
- **Episode counts carry the season they sit in.** Three episodes measured
  against a two-season total reports 25% and reads as abandonment; the same
  three were half of a self-contained first series. The evidence says which
  season the plays are in.
- **Distilled answers now carry the rules that produced them.** A cached
  cultural-significance value was a verbatim cast list — but re-running
  today's prompt on the same article produced the awards, three times out of
  three. The entry was months old, written under weaker rules, and "checked"
  meant "never looked at again". Distillations are stamped with a version
  derived from the prompt text, so editing the prompt retires the old
  answers by itself; the background walker offers stale ones again.
- **Community reception is gathered before the verdict, not after.** It was
  left to a background walker, so a title could be judged with none on file
  while the follow-up discussion — which does fetch it — argued from better
  data than the verdict it was questioning.

## 2026-08-21 — Beta hardening

**Version.** The app reports `1.0.0-beta`, and says so in the UI. The
number is declared in one place instead of being repeated across two dozen
file headers that had already drifted apart.

**Play counts mean what they claim.** Replays were counted from raw history
rows, which mistook two things for a rewatch: an episode logged once
partway through and again when it finished, and repeated *abandoned* starts
of an episode that was never completed once — the exact inverse of the
signal. Views are now filtered by completion and collapsed when they fall
inside the same viewing window, and abandoned starts are reported
separately.

**Resumed views no longer leave two rows.** Plex reports a partial view and
the later finished view through two different queries; only the first was
being reconciled. The finished view now promotes the unfinished row it
belongs to. `scripts/dedupe_watch_history.py` repairs what earlier syncs
already wrote — dry run by default, and it only ever removes unfinished
rows.

**Duplicate detection respects external-id namespaces.** TMDB numbers films
and series in separate sequences, so the same number identifies two
unrelated works. Grouping on the bare id paired them and reported the pair
as one title stored twice, quoting the other work's size as the redundancy.
Duplicate lookups and technical-profile lookups are now scoped by media
type, and an unknown type is refused rather than guessed. Genuine
duplicates are unaffected.

**Listening depth counts listens, not skips.** Deletion protection for
music treated every history row as evidence of devotion, including tracks
that were skipped. It now requires a play that finished or ran past two
minutes — the same rule the import path applies. Heavy rotation is
unaffected; the change is felt only at the margin, which is precisely where
deletion candidates live.

**Non-fiction is judged as non-fiction.** A documentary was being measured
against the criteria of prestige drama. The judge now receives an explicit
form line for factual work: research depth and clarity are the yardstick, a
known outcome is the point, and a conventional structure is craft.

**Background text has a deterministic language.** Deletion pitches are
generated with no conversation attached, and language detection fell back
to scanning unrelated chat history — so the same title could be pitched in
one language and re-evaluated in another. Surfaces with no live message now
answer in English; chat still follows the language it is spoken to.

**Fewer round-trips.** Deletion scoring, library ranking, cache
invalidation, the downscale list and the backlog page all issued one query
per item. Each now issues one query per batch — the backlog page measured
35× faster on a full page. Its top-three tie-break is spelled out rather
than left to whatever order the database returned. Metadata lookups against
the optional LAN neighbour run concurrently, and a partial result is no
longer cached as though it were complete.

**Housekeeping.** Search evidence scoring was split into named steps with
scores verified unchanged against the ground-truth matrix. Markup no longer
leaks into generated prose. The test battery pins UTF-8 for child
processes, so a suite is no longer reported as failing because of the
terminal it was launched from. Runtime state and coding-agent scratch files
stay out of the repository.

## 2026-08-20 — Public release

- **Went public** under [AGPL-3.0](LICENSE); ported components remain under
  their original licenses (see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)).
- **Performance:** deletion scoring and library ranking no longer issue one
  vector-store call per item, and enrichment timestamps load as column
  tuples instead of full ORM objects.
- **Security:** response-header middleware (`X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`), implemented as pure ASGI so
  server-sent event streams stay untouched.
- **Accessibility:** labels on icon-only buttons and unlabeled inputs;
  decorative graphics hidden from screen readers.
- **Tests:** one battery command — `python tests/run_all.py` — runs every
  suite, and CI runs exactly that. Several suites that silently executed
  nothing under the old convention now execute for real.
- **Docs & packaging:** the README became a project front page, with daily
  operation moved to an [operator guide](docs/USAGE.md) and internals to
  [ARCHITECTURE.md](ARCHITECTURE.md). Added `.env.example`,
  `CONTRIBUTING.md`, `SECURITY.md` and a pinned `requirements.txt`.

## 2026-08 — Curation quality: search, judge, integrity

- **Curated semantic search.** The model only parses the query (anchor and
  constraints); scoring is deterministic against real metadata tags —
  lexical-first matching, concept and tone families, negation, demographic
  and comedy guards, per-constraint evidence notes, and an honest coverage
  banner when nothing carries the full profile.
- **Multi-vector theme facets.** Each title's theme phrases become
  individual points in a separate collection, so a query combining opposed
  qualities can match a title in both regions instead of searching the
  meaningless middle.
- **4-pillar deletion judge.** Taste, household use, custodianship and
  resonance, ruled per title from verified evidence — with learned curation
  principles extending the constitution, durable protections for what it
  keeps, and a thin-evidence gate so titles without enrichment data are
  skipped rather than judged blind.
- **Two-bake model split.** Conversation and batch deletion pitches run on
  separately baked model tags, with residency-guarded VRAM eviction.
- **Background-job visibility.** Every maintenance task reports live
  progress, and a test freezes the audited set so new jobs are visible by
  default.
- **Data-integrity sweep.** Domain migration, tri-state significance so
  transient failures no longer stamp a permanent verdict, self-healing for
  orphaned vector documents (rebuilt deterministically from cached data, no
  model involved), an adult-genre merge guard, and corrupt-source-id
  detection.
- **Robustness layer** (ported, MIT): durable entity pins with a "Fix match"
  action for same-named-work collisions, playlist reconcile instead of
  delete-and-recreate with stale-key self-heal, and a mass-staleness guard
  so an unreachable service is never mistaken for a deleted library.

## 2026-06 — Grounding, learning, multi-user recovery

- **Verified-data evidence everywhere.** Pitches, discussions and
  re-evaluations reason from cached facts — creators, plot, themes, awards,
  cultural significance — instead of model memory.
- **Entity resolution.** Year and MusicBrainz-id disambiguation so
  same-named works are judged as themselves; watch status comes from real
  history.
- **A curator that learns.** Keep-feedback becomes standing considerations
  that protect similar titles in future proposals; episodic memory
  extraction with semantic-similarity conflict handling.
- **Platform work.** Embedding-stack v2 (prefix-aware model, new
  collection, eval-gated migration), concurrency lifecycle hardening, and
  recovery flows for deleted library sections and re-attributed users.

## 2026-05 and earlier — the hardening passes

An audit-driven refactor arc that took the codebase from prototype to a
working household deployment:

- Critical-path fixes, multi-user attribution, settings surface,
  concurrency lifecycle, listening-history import.
- Music pipeline cascade (library match → id resolve → genre fill →
  fallback), scheduler missed-job replay, service caches, synopsis browser,
  add-new flow.
- Anime id mapping bridge, vector-based recommendations, game-mode VRAM
  hand-off, scheduler reliability.
- Numeric correctness fixes, a media-identity store, a server-side game
  watcher, protection-intent detection.
- Negative caching, proposal re-evaluation, user ratings folded into taste,
  database-lock cascade fixes, and the unified per-library breakdown panel.
