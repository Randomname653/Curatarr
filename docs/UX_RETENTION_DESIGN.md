# Curatarr — Feeling at home: UX, retention and habit design

_Design record, **Proposed** 2026-11-08. Audit of commit `38b3f28`; line
numbers describe that commit. Scope: what a household member (not the
owner) sees in Home/Chat, Swipe, Wrap-Up and the bell, and how the TV
rows and the web UI could form one calm loop._

The goal is **intrinsic** pull: the member feels understood, finds the
evening's pick in under a minute, and looks forward to their own
retrospectives. No dark patterns: no push spam, no streak guilt, no
"you missed" counters, no artificial scarcity. Every card is dismissible
and every proactive surface stays behind the existing per-trigger
switches in Settings → Notifications.

The repo's standing laws apply unchanged: **the LLM never invents facts**
(verified data in, register-honest prose out); **the GPU serves one
curator generation at a time**, so a page load must never cost an LLM
call; **judge the work, never the person** (`curator_voice.py:44-52`).
Most proposals below are therefore deterministic and reuse data that
already exists.

---

## 0. Summary

Curatarr's engineering is far ahead of its hospitality. Many pieces
that keep people coming back already exist, often quite polished:

- curator-voiced, fact-anchored, decaying chat starters (`chat_starters.py`);
- a "how did it land?" follow-up for titles watched from the Recommended
  playlist (`proactive_messages.py:870-871, 1434-1440`), and a "Morning
  line" about last night (`:806-860`);
- a tactile swipe deck with haptics, undo and an immediate watchlist write
  (`swipe.js:28-59, 501-507`);
- a full-screen Wrap-Up story with an editor/writer pipeline and fact checks
  (`wrapup_story.js`, `wrapup_writer.py`);
- deep links into Plex and Jellyfin (`plex_adapter.py:181`, `jellyfin_adapter.py:262`).

Members don't see most of it, or they meet it in the wrong place, at
the wrong moment and in machine-room language. Four problems stand out:

1. **The landing page speaks to the owner.** A member is greeted with
   "how to clean up your library", an enrichment percentage, and buttons
   for syncs and Knowledge-Base pipelines.
2. **The loop closes in the bell.** The post-watch follow-up exists, but it
   lives behind a 60-second-polled bell icon, labelled with an internal
   trigger name, and answering it means typing in a chat.
3. **Talk and action are disconnected.** A chat reply is plain text with
   bold titles. You can't play a title, put it on the watchlist or keep it
   from the reply. The recommendation cards have only "Discuss".
4. **Nothing ends, so nothing feels finished.** The swipe deck refills
   forever, its progress ring counts a metric most answers never move,
   and the Wrap-Up has no week or month to look forward to.

---

## 1. Diagnosis

### 1.1 Home and first impression

| # | Finding | Where |
|---|---|---|
| H1 | **The landing view is the chat, and its greeting is the owner's.** "Ask me what to watch, how to clean up your library…" plus the chip "What can I clean up in my library?" for members, who cannot open Deletions at all (`ADMIN_VIEWS`). | `index.html:278-287`, `nav.js:62, 133-136` |
| H2 | **Machine-room numbers on the member landing.** Glance tile "`NN%` Library enriched · `NN%` of watched titles". "`N` Recommendations ready" is capped at 32 by the query, and when the member has no cache yet it falls through to an on-the-fly LLM generation, so a page load can take the GPU. | `chat.js:355-425`, `recommendations.py:147, 170-184` |
| H3 | **Admin chrome visible to members.** Nav: Knowledge Base ("metadata from TMDB, AniList and MusicBrainz", "AniList ready" counts), Activity ("Live background tasks and automation history"). History view: "Sync now", "Recompute taste", "Plex libraries" (which opens an admin-only pane), and stats "Entries synced / In taste profile / Profile status". The topbar shows a "beta" tag, a "Game mode" indicator, and a "Re-fetch" button whose tooltip names TMDB/AniList/MusicBrainz. | `index.html:128-141, 160-231, 294, 308-313`, `history.js:34, 58-63`, `kb.js:37` |
| H4 | **Member-facing error strings name the infrastructure.** "Ollama not reachable at {url}. Make sure Ollama is running and '{model}' is pulled." and "Curatarr is busy (chat starters) — you are next in line…" (the gate owner's internal name). | `chat.py:3466-3471, 3677-3679` |
| H5 | **No sense of time.** No greeting by daypart or weekday anywhere in the UI. The starters do have dayparts, but they read the **server** clock (`datetime.now()`), not the member's zone. The browser reports that zone at login and `user_time.py` already converts with it, yet only `wrapup_facts.py:267` uses it. In a UTC container (the Docker default) "evening" starts at 18:00 or 19:00 local in Germany, and "night" ends an hour or two late. The `last_night` message has the same flaw. | `chat_starters.py:95-103, 263, 453, 522`, `auth.js:203-209`, `proactive_messages.py:811` |
| H6 | **The cold start is four "come back later"s.** A new member gets no first-run flow ("should still land straight in the app", `auth.js:264-269`). Starters need at least one fact besides the clock, so they get none (`chat_starters.py:367`). Swipe says "Watch a few titles in Plex first, then come back" (`swipe.js:264`), which blocks the one surface that could build a taste profile in 60 seconds. Recs: "about 2–4 minutes the first time" (`recs.js:262`). Wrap-Up: "Not enough yet" (`wrapup.js:97-99`). | as cited |
| H7 | **Returning members land in yesterday's argument.** `restoreChatHistory` paints the whole `general` thread above the landing panels and scrolls to the bottom. The returning member sees the tail of an old conversation, not "what's for tonight". | `chat.js:651-672` |
| H8 | **The UI is English only**, while the curator answers in the member's language. Starter batches are generated in English by design (`chat_starters.py:36-38`), so a German-speaking member gets English chips followed by German replies. | `index.html:2`, `ui.js:84-89` |

### 1.2 Swipe

What already works well: six reactions with directions, keys and long-press
stars; tilt, stamp and fly-out physics; haptics that respect reduced motion;
undo; a Loved answer chains three neighbours in behind the current card;
taste-matching tags are highlighted (`swipe.js:28-59, 516-518, 600-678`,
`swipe.py:175-178`). The friction:

| # | Finding | Where |
|---|---|---|
| S1 | **No deck, no ending.** It fetches 8 and refills at 5 forever. "Done" only means the 200 nearest neighbours per centroid are used up, and the done screen reads as exhaustion ("You've been through everything near your taste…"). It does not count watchlist adds and offers no "play one now". | `swipe.js:56, 259, 476, 276-287`, `routers/swipe.py:79-81`, `swipe.py:39` |
| S2 | **The progress ring measures the machine, not the person.** It counts only *seen* answers toward the next taste rebuild ("`n` to go · seen-it answers until your taste profile refreshes"). The server's own comment says unseen answers are about 96 % of all answers, so the ring barely moves. | `swipe.js:169-185`, `swipe.py:37, 289-293` |
| S3 | **Monotone order.** Strictly nearest first, with no diversity pass, so ten near-identical cards in a row. That is where fatigue starts. | `swipe.py:214` |
| S4 | **The hook is weak.** "Near your taste for **A**, **B**, **C**" names a cluster centroid's top three titles, which is a statistical explanation rather than a reason. The card carries a 600-character synopsis (clamped to 3–6 lines), but no one-line vibe. | `swipe.js:376-380, 407-409`, `swipe.py:76, 156` |
| S5 | **A like has no visible consequence.** "Want to watch" writes to the watchlist immediately and says so (`swipe.js:502`), which is good. Liked and Loved only store a row. They reach the TV through a taste rebuild every 10 seen answers, then the weekly recs refresh (`cadence_h` 168), then the daily playlist push. Nobody is told. | `swipe.py:287-314`, `data_custodian.py:877-880`, `plex_playlists.py:405-417` |
| S6 | **Server wording is hardcoded.** "is on your Plex watchlist", "not added to your Plex watchlist", "Watch a few titles in Plex first" also appear on Jellyfin. `serverLabel()` already exists in `state.js:33-35`. | `swipe.js:264, 502, 505, 507` |
| S7 | **Undo half-undoes.** The rating goes, but the watchlist entry stays ("it stays on your Plex watchlist"). | `swipe.js:551-554` |

### 1.3 Curator chat

| # | Finding | Where |
|---|---|---|
| C1 | **The prompt has rules, not a conversation shape.** Seven defensive rules (topic lock, no invention, no library actions, no monologue, analytical integrity, size sense) and "do not create walls of text". Nothing covers how many titles to suggest, how long a recommendation turn should be, or handing the ball back. With `num_predict` 4096 a "what should I watch?" can come back as a listicle. | `chat_prompt.py:146-187`, `chat.py:3569-3576` |
| C2 | **"What should I watch tonight?" searches for that sentence.** Free chat retrieves 5 vector hits for the literal user message (`retrieval_query = message.message`). The member's cached recommendations, active series, watchlist and swipe "Want" list are never offered to the curator as tonight's candidates. It answers from whatever the sentence embeds near. | `chat.py:3330-3333`, `semantic_search.py:275-284` |
| C3 | **Starters are good, but one-sided and fragile.** They are curator openers only (by design, `chat_starters.py:21-30`), and three at most are shown. User-voiced chips stay the three static placeholders, one of them admin-only (H1). New members get none (H6), they read the server clock (H5), and they are English only (H8). The facts cover history and milestones, but nothing about *tonight*: no runtime budget, no unwatched picks, no watchlist. | `chat_starters.py:145-265`, `chat.js:467-512` |
| C4 | **No actions in a reply.** The stream sends only `thread / status / warning / token / done`. Titles are just `**bold**` (`chat_prompt.py:185`). You can't open a title in Plex/Jellyfin, add it to the watchlist, keep it, or swipe-rate it from the reply. | `chat.py:3451-3726`, `chat.js:290-311` |
| C5 | **"Please never delete this" works invisibly and late.** A background detector runs after the turn, and the real outcome (protected, watchlist add tried) arrives as a bell note (`chat.py:826-905`). There is no explicit Keep button, so the member cannot know it worked while still in the conversation. | as cited |
| C6 | **Deep links exist and are unused for members.** Both adapters build them, but only the owner's duplicate and music views use them. | `plex_adapter.py:181-188`, `jellyfin_adapter.py:262`, `recommendations.py:853, 2709`, `curation.js:309` |

### 1.4 Wrap-Up

What already works well: a full-screen story with tap and hold, a quiz,
reveal, rhythm, clock, roast/toast and identity slides; an archetype written
by an editor/writer pipeline that checks every number; per-tile share
sentences; snapshot cards (`wrapup_story.js:242-348`, `wrapup_writer.py:468-506`).
The friction is about *when* and *how you get there*, not the content:

| # | Finding | Where |
|---|---|---|
| W1 | **Its only door is the sidebar.** The rolling 30-day story is pre-written weekly and the year story from 1 December, but no surface tells the member. | `index.html:178-180`, `wrapup_story.py:440-469` |
| W2 | **No calendar week or month.** Periods are `30d`, `6m`, `all`, `year:`, `season:`. "Last 30 days" is not "your October", and nothing exists at week scale. | `wrapup_facts.py:98-118`, `wrapup.js:16` |
| W3 | **The narrative facts stop short of rituals.** Night owl, weekend matinee, binger and comeback exist. Missing: a per-weekday or daypart *ritual* ("Tuesdays after 21:00 were anime"), a comfort **genre** (comfort is per title), an artist, title or genre "of the month", and a first-play-of-the-period beat (`first_last` reaches only the LLM fact sheet). | `wrapup_facts.py:423-469, 615-635, 1128-1180`, `wrapup_story.py:102` |
| W4 | **The playlist is a toast.** "Make it a Plex playlist" creates it on demand and answers `On Plex now: "{name}" ({n})`, with no link. The wording says Plex on Jellyfin too, and Jellyfin's `skipped: no_jellyfin_account` falls through to "None of these titles were found in Plex". | `wrapup.js:164, 505-508`, `routers/wrapup.py:305-342` |
| W5 | **Sharing is text only.** Every slide defines a `share:` string that nothing reads. The only share action is on the last slide. | `wrapup_story.js:247, 261, 267, 343-346`, `wrapup.js:544-562` |
| W6 | **The fallbacks read like a report.** "The period in hours", "Where the hours went", "When you watched". A scope other than "all" has no pre-written story, so it waits behind "Let the curator write it". | `wrapup_writer.py:51-54`, `wrapup.js:162` |

### 1.5 Habit loop

| # | Finding | Where |
|---|---|---|
| L1 | **The TV half of the loop exists.** "Curatarr Recommended" playlists and the designed collections sit on the server's home screens, and `plex_sync._process_rec_watch_hits` notices when a member watched one. | `plex_playlists.py`, `plex_collections.py`, `plex_sync.py:1088, 1151` |
| L2 | **The web half breaks.** The follow-up becomes a bell message, shown one at a time, with the badge text `trigger_type.replace('_',' ')`. Only the first underscore is replaced ("history deep_dive"), and members see internal names. Some trigger labels grade the *person*, which `curator_voice.WORK` forbids: "Attention deficit", "Procrastinator", "Guilty pleasures", "Genre rut", "Low completion". Answering means Respond → chat → typing. | `notifications.js:75-90, 81`, `proactive_messages.py:868-904` |
| L3 | **Feedback has no visible effect.** Nothing says "got it, more like this moved up". The member's verdict disappears into memory extraction. | `proactive_messages.py:870-871` |
| L4 | **The bell polls and nothing pushes.** That is correct and should stay: the loop should be pulled by the TV row and the member's own curiosity, never pushed. | `auth.js:256-258`, `on_this_day.py:1-6` |

---

## 2. Concept

### 2.1 Home: the living room and the machine room

**Principle:** members get a *living room*; the owner keeps the machine room
one click away. Role decides the default and never hides the owner's tools.

**A. "Tonight" header above the chat** (members and owner; the owner can
collapse it). Deterministic, no LLM, computed from data that exists. A new
endpoint `GET /api/home/tonight` returns at most three cards in this order:

1. **Follow-up card** (§2.5): "How was **{title}**?" with four one-tap
   reactions. Shown only for a play in the last 36 h (member's zone),
   at most one per visit, and it expires like `last_night` does.
2. **Tonight pick**: one title with poster, why line, runtime, and the buttons
   **▶ Play on {server}**, **+ Watchlist** and **Something else**. The rules use
   the member's local daypart and weekday (`user_time.to_local`):

   | Moment | Pick from | Copy |
   |---|---|---|
   | Mon–Thu evening | next episode of the active series (`series_progress`); else an unwatched episode-length title ≤ 30 min | "A short one for a {weekday}: **{title}**, {runtime} min" |
   | Fri/Sat evening | top library-lane film from `CachedRecommendation` | "Friday — room for a film: **{title}**" |
   | Sun afternoon | a Rediscover-lane title or a comfort rewatch | "Sunday rewatch? **{title}** — you rated it {stars}" |
   | Night (after 23:00) | a short, low-intensity title or the night-time series the member usually watches | "Still up? Something short: **{title}**" |
   | Morning/day | the music rotation, or the unwatched title waiting longest on their watchlist | "For later: **{title}** is on your watchlist" |

   The copy comes from fixed templates and every slot is a fact, so the
   no-invention law holds without an LLM.
3. **One soft entry:** "Your week" on Monday and Sunday (§2.4), else the swipe deck
   for a new member ("Teach me your taste — 60 seconds"), else
   "**N** picks from your watchlist are waiting".

**B. Greeting by daypart**, from the member's zone: "Good evening, {name}" /
"Late one, {name}" (in the UI language, see H8). Replace the static welcome
bubble (`index.html:278-280`) with it, and drop "clean up your library" for
members.

**C. Remove the admin-only items from the member landing:**
- Glance tiles: members get "Watchlist · N waiting", "New picks since {day}"
  and "This week · N h". Enrichment % and "Last activity" go to the owner only.
  The rec tile must read the cache only and never trigger generation (H2).
- Static chips: replace "What can I clean up…" for members (see §2.3 B).
- Nav: show Knowledge Base and Activity only to the owner. Members who want
  them can open them from Settings → "Behind the scenes".
- History view: "Sync now / Recompute taste / Plex libraries" go to the owner
  only. Rename the stats to "Plays counted · Shaping your taste · Profile up to date".
- Topbar: hide "beta", "Game mode" and "Re-fetch" for members. Re-fetch moves
  into the message's overflow menu.
- Member-facing errors: "The curator is resting right now — try again in a
  minute." The log keeps the detail. "Busy (chat starters)" becomes
  "One moment — someone else is talking to the curator."

**D. Returning visit:** open on the Tonight header with the old thread
**collapsed** behind "Continue our last conversation ›". Don't load it into the scroll.

**E. Cold start (the first 60 seconds of a new member):**
1. The greeting says what Curatarr is in one sentence: "I learn what you like from
   what you watch — and from a few quick taps."
2. A primary button opens a **calibration deck** (§2.2 D): 10 well-known titles
   from the library, rated with "Seen it — loved / liked / meh / not for me" or
   "Haven't seen it". Seen answers already feed the taste engine
   (`swipe.py:361-378`, `taste_engine.py:1134-1143`).
3. After the deck: "Got it — your first picks are being made. They'll be on
   your {server} home screen as 'Curatarr Recommended'." Queue a per-member recs
   generation at background priority. The existing "2–4 minutes" note stays
   in the Recs view only.
4. The chat chips are user-voiced and time-aware from the first second
   (§2.3 B), so the empty input field never stands alone.

### 2.2 Swipe: a flow with an ending

**A. Decks, not a feed.** One "deck" = 10 cards (configurable: 10/20/∞).
- The ring shows **deck progress** (`3 / 10`) instead of rebuild progress. The
  taste-rebuild note moves to a small line on the end screen.
- Deck types in the category tabs' header:
  - "Tonight deck": unwatched titles in the library that fit the daypart and runtime rules of §2.1;
  - "Quick 10": the current behaviour;
  - "Calibration": §2.2 D;
  - "From my Loved": chains on the last Loved title.
- Refill stays as it is inside a deck. At 10 the deck ends deliberately.

**B. End screen = reward + action.**
```
  ✓ Deck done — 10 answered in 1:12
  3 on your Jellyfin watchlist        [▶ Play the first one]  [Open watchlist]
  You loved "The Lantern Room" → 3 more like it lead your next deck
  Your taste profile refreshes after 4 more "seen it" answers
  [Another 10]   [Done for tonight]
```
The deck-end summary is built in the client from `_history`; the play button
uses the adapter's `deep_link`. "Done for tonight" is a respectful exit: no
nag, no streak.

**C. Card hook, readable in 2 seconds** (top to bottom):
1. **Reason line from one anchor title:** "Because you loved **X**". X is the
   member's rated or completed title nearest to the card's vector. Use the
   chained title when there is one, else the nearest of the cluster's top
   titles (already loaded in `swipe.py:76`). Drop the three-title centroid phrase.
2. Title · year · runtime ("1 h 52" / "24 min episodes") · the score that exists.
3. **One-line pitch, at most 140 characters:** the first sentence of the
   enriched `embedding_text` or synopsis, cut at a sentence boundary, plus up
   to 3 mood tags as pills. The full synopsis opens on tap, as it does now.
4. Two taste pills ("Part of your taste") stay. Cast and director move behind the tap.

**D. Calibration deck for cold start.** If there is no taste profile, replace
"Watch a few titles in Plex first" (`swipe.js:264`) with a deck of library
titles ranked by household plays and a high public score, so no profile is
needed. Only *seen* answers count. After 5 seen answers, trigger the first
taste build. Ending a calibration deck is the moment to point at the TV row.

**E. Variety:** a cheap MMR-style re-rank of the 200-neighbour pool
(`swipe.py:214`). At most 2 of any one genre or cluster in a row, and every
fifth card from a second cluster. No new data is needed.

**F. Visible effect of Liked/Loved:** a pill "Loved — I'll lead with titles
like this" on the card. Loved also moves its three chained neighbours to
the top of the member's library lane in `CachedRecommendation` (a re-rank,
not a regeneration). The next daily playlist push then reflects it within
24 h instead of a week.

**G. Small fixes:** `serverLabel()` in every watchlist string (S6). Undo also
removes the watchlist or playlist entry it created, or says plainly that it
stays (S7).

### 2.3 Curator chat: from Q&A to a conversation

**A. Conversation-shape rule.** Add it to `chat_prompt.py` as rule 8, active
only in free chat. It must not apply in deletion or principle discussions,
where the analytical rules govern.

```
8. CONVERSATION SHAPE (free chat): You are a friend behind the counter of a
   good video and record shop, not a catalogue.
   - When the user asks what to watch or hear: offer ONE or TWO titles, best
     fit first. For each, one sentence on why it fits THIS user — name one
     concrete link from the context (a title they rated or finished, a theme
     in the taste summary). Prefer titles from [TONIGHT CANDIDATES], which the
     user owns and has not watched.
   - Then hand the ball back with ONE short question that narrows the next
     pick: length, mood, company, or "closer to A, or stranger like B?".
   - List more only when the user asks for a list.
   - Any other turn: answer first, in at most about 120 words unless the user
     asks for depth. End with a question only when it moves the conversation.
```
Measure it with the existing `tests/benchmarks/tone_bench.py` and
`tests/benchmarks/curator_bench.py` before shipping. The bench builds turns with `build_system_prompt`, so
it measures the real prompt (`chat_prompt.py:1-9`). Add fixtures: "what should
I watch tonight" must give ≤ 2 bold titles and end with `?`. Then lower
`num_predict` for free-chat recommendation turns, for example to 600.

**B. User-voiced chips beside the curator's openers.** The landing shows up to
3 curator openers (as now) **plus** up to 3 user-voiced chips. The user-voiced
chips come from deterministic templates filled with facts from
`collect_facts`. There is no LLM, so they work from the first second and in
the UI language:

| Fact present | Chip (inserts into the input, user can edit) |
|---|---|
| `last_watched` = X | "Something like **X**, but lighter" |
| evening, weekday | "Something under 30 minutes" |
| Fri/Sat evening | "A film for tonight — surprise me" |
| watchlist non-empty | "Pick one from my watchlist" |
| `music_rotation` = A | "More like **A** for the background" |
| always | "I'm in the mood for …" (cursor after "for") |

The admin-only chip "What can I clean up in my library?" shows for the owner only.

**C. [TONIGHT CANDIDATES] context block.** On a free-chat turn whose intent
is "recommend" (a cheap regex plus the existing domain inference,
`chat.py:915`), inject up to 8 candidates:
- the library-lane `CachedRecommendation` rows with their stored reason;
- the next episode of active series;
- the oldest watchlist adds and recent swipe "Want" answers.

Each line carries runtime, rating key, watched flag and the stored `reason`.
That is "verified data in", it costs no extra LLM call, and the curator stops
recommending from the embedding of the sentence "what should I watch" (C2).

**D. Actions under every reply (zero friction).** This is a deterministic
post-pass with no tool calling. When the stream ends, the server matches the
reply's `**bold**` titles against the library index (`_match_library_title`,
`chat.py:515`) and sends one frame:
```json
{"titles": [{"title": "The Lantern Room", "category": "movie", "rating_key": "4711",
             "in_library": true, "watched": false, "on_watchlist": false,
             "deep_link": "https://…", "protected": false}]}
```
The client renders a compact action row under the bubble for each matched title:
- **▶ Play on {server}**, using `deep_link`;
- **+ Watchlist**, using the same `add_to_watchlist` path as Swipe;
- **Keep**, an explicit protection: the existing protection write, confirmed
  in place ("Kept — it won't be proposed for deletion");
- **Discuss**, which anchors the thread on that title.

Not-in-library titles show **Wish** (member) or **+ Add** (owner), as on the
recs cards. This also fixes C5: the background detector stays the safety net,
but the member gets an explicit button and an immediate confirmation.

**E. The same action row on recommendation cards** (`recs.js:96-102`):
Play, Watchlist and Discuss for library lanes. Today a library rec can only be discussed.

**F. Starters, small fixes:** daypart and weekday from the member's zone (H5);
generate an extra small batch on the first visit after the calibration deck,
which gives them facts; keep English batches but let a German member see the
UI-language chips (B) first.

### 2.4 Wrap-Up: from stats to stories, and moments to look forward to

**A. Micro-wraps.**
- **"Your week" card**, Sunday evening to Monday, on the Tonight header. It is
  deterministic and built from `build_wrapup_facts` with a new `7d` key, or
  better `week:YYYY-Www` in the member's zone. It has three lines and no LLM:
  ```
  Your week · 9 h · 5 evenings
  Tuesday was anime night (3 episodes of Starfall Archive after 21:00)
  New for you: a first Korean thriller — The Quiet Harbour ★★★★★
  [See the week]  [Make it a playlist]
  ```
- **"Your month" mini-story** on the 1st, using a new `month:YYYY-MM` key. It
  has five slides (open, number one, ritual, new discovery, identity). The
  `wrapup_pregen` job writes it at background priority, so it is ready when
  the member arrives (W1).
- The yearly story keeps its December pre-write, and gets a Tonight-header
  card "Your 2026 is ready" from 1 December.

**B. New narrative facts** (deterministic, in `wrapup_facts.py`):
- **ritual:** the same weekday × daypart with the same series, genre or
  category on ≥ 3 different weeks in the period → "Your Tuesday-night anime".
- **comfort genre:** the genre with the highest share of rewatches and
  completed plays → "Your secret comfort genre: heist films".
- **of the month:** the top artist, series or genre per calendar month in a
  year period → "September belonged to The Hollow Pines".
- **night shift:** a series with ≥ 50 % of its plays after 23:00 local → "Your after-midnight series".
- **first of the period:** "It started with…" from `first_last`.

The writer gets them as cards like any other. The editor may open with a
ritual instead of hours, since identity beats volume.

**C. Memories you can touch.**
- The playlist call returns the playlist's deep link. The toast becomes a
  card: "On {server}: Curatarr Wrap-Up · October (12) [▶ Play]".
- Handle Jellyfin's `no_jellyfin_account` with a clear message, and use
  `serverLabel()` on the button.
- Offer the week and month playlists from their cards with one tap.

**D. Share images:** render the current slide to a 9:16 or 1:1 PNG (canvas,
client-side) with the slide's existing, unused `share:` text (W5). This is
already listed as a v2 item in WRAPUP_V2_BLUEPRINT §4.2.

**E. Warmer fallbacks:** replace "The period in hours" style templates with
templates built from two facts side by side, matching the writer's own rule
(`wrapup_writer.py:482-506`): "9 hours, mostly after 22:00".

### 2.5 The habit loop: TV ⇄ Curatarr

```
   ┌──────────── TV home screen (trigger) ─────────────┐
   │ "Curatarr Recommended" playlist · designed         │
   │ collections · watchlist (fed by Swipe "Want")      │
   └───────────────┬───────────────────────────────────┘
                   │ member plays X
                   ▼
   plex_sync / Jellyfin poll notices the hit (exists)
                   │
                   ▼  next time Curatarr opens (any surface, member's zone)
   Tonight header: "How was X?"  [Loved] [Liked] [Meh] [Not for me]   ← 1 tap
                   │
                   ├─► stored as a post-watch verdict (taste signal, weighted
                   │   like swipe stars), and as the recommendation_followup outcome
                   ├─► immediate visible echo: "Got it — 3 titles like X moved up"
                   │   (re-rank of the cached library lane, no LLM)
                   └─► optional: "Tell the curator why" → opens the chat thread
                                                     with the follow-up as opener
                   │
                   ▼
   next daily playlist push carries the re-ranked picks → TV row has changed
```

**Mechanics:**
- **Card source:** the existing `recommendation_followup` and `last_night`
  triggers already find the play. Instead of only writing a bell message,
  `GET /api/home/tonight` exposes the newest unanswered one as a card. The bell
  copy stays for members who prefer it. A card answered on Home marks the bell
  message read, and the reverse.
- **Storage:** a small `watch_reactions(user_id, history_id, title, category,
  stars, at)` table, or a "post-watch" source on `swipe_ratings` if the taste
  engine's "already played → skip" rule (`taste_engine.py:1134-1143`) is
  relaxed for that source. The current behaviour drops seen-and-played titles
  so they don't count twice. A post-watch verdict is new, explicit evidence and
  should count. Decide this when building it.
- **The echo must be true.** Show "moved up" only when the re-rank really
  moved something.
- **Weekly rhythm:**
  - Monday: "Your week".
  - The 1st: "Your month".
  - Evenings: the Tonight pick.
  - Mornings: "Morning line" (as now, in the member's zone).

  That is at most one new reason to open Curatarr per day, and none of them is pushed.

**Guardrails:**
- No OS push, no e-mail, no streak counter, no "you haven't been here in N days".
- Every card has a ✕ and remembers it for that title.
- Every follow-up card stays behind its Settings → Notifications switch.
- Rename the trigger labels that judge the person:

  | Current | New |
  |---|---|
  | Attention deficit | Started, not finished |
  | Procrastinator | Waiting on your list |
  | Guilty pleasures | Comfort picks |
  | Genre rut | Same lane lately |
  | Low completion | Left halfway |

  Render badges from the label, not from `trigger_type` (L2).

---

## 3. Implementation plan

Effort estimates assume one developer who knows the codebase. Every step
names the tests it needs. `tests/test_app_context_drift.py` pins UI labels
the curator references, so renamed buttons must be updated in `app_context.py`
too.

### Stage 1 — Quick wins (about 1–2 hours each, no schema change)

| # | Change | Files | Check |
|---|---|---|---|
| 1.1 | **Conversation-shape rule** (§2.3 A) in `build_system_prompt`, free chat only. Lower `num_predict` for recommendation-intent turns. | `chat_prompt.py`, `chat.py:3569-3576` | tone/chat bench; new fixture: ≤ 2 titles, ends with a question |
| 1.2 | **User-voiced, time-aware chips** (§2.3 B), deterministic and built from `collect_facts` plus the member's zone. Hide the admin-only chip for members. | `chat_starters.py` (new `user_chips()`), `routers/chat.py:/starters`, `chat.js:477-487`, `index.html:284-288` | unit test: zone-aware daypart, no admin chip for members |
| 1.3 | **The member's clock for dayparts**: `current_daypart` and the weekday from `user_time`, in starters and `last_night`. | `chat_starters.py:95-103, 263, 453, 522`, `proactive_messages.py:811` | test with a UTC server and an Europe/Berlin member at 17:30 UTC |
| 1.4 | **Remove admin chrome for members** (§2.1 C): glance tiles; nav KB and Activity; History buttons; topbar beta, Game mode and Re-fetch; greeting copy; the rec tile reads the cache only. | `chat.js:355-425`, `index.html`, `auth.js:243-249`, `history.js`, `recommendations.py:170-184` | role snapshot test of visible elements |
| 1.5 | **Member-friendly error and busy strings** (H4); the log keeps the detail. | `chat.py:3466-3471, 3677-3679, 3699` | — |
| 1.6 | **Follow-up card on the landing** (simple version): show the newest unread `recommendation_followup` or `last_night` message as a card above the chat with four reaction buttons. Each button sends a fixed, user-voiced verdict ("Loved it" / "It was fine" / …) through the existing Respond → thread path, so the existing memory/feedback path stores it. | `notifications.js`, `chat.js`, `index.html` | manual run plus one e2e |
| 1.7 | **Badge and label hygiene**: badges from `label`, not `trigger_type`; rename the person-judging labels (§2.5). | `notifications.js:81`, `proactive_messages.py:868-904` | — |
| 1.8 | **`serverLabel()` everywhere** in Swipe and Wrap-Up strings (S6, W4). | `swipe.js`, `wrapup.js` | grep test: no literal "Plex watchlist" in member strings |
| 1.9 | **Collapse the old thread** on a returning visit (H7). | `chat.js:651-672` | — |

### Stage 2 — Swipe and action flow (about 1–3 days)

| # | Change | Files |
|---|---|---|
| 2.1 | **Action frame after a chat reply** (§2.3 D): match bold titles, send a `titles` frame, render Play / Watchlist / Keep / Discuss. | `chat.py` (after `_persist_turn`), new `chat.js` renderer, adapters' `deep_link` |
| 2.2 | **Play and Watchlist on recommendation cards** for library lanes; `deep_link` in the recs payload. | `recs.js:83-113`, `recommendations.py` |
| 2.3 | **[TONIGHT CANDIDATES] block** for recommendation-intent turns (§2.3 C). | `chat.py:3330-3333`, new helper beside `because_recs` |
| 2.4 | **Decks of 10** with deck progress, a deck-end screen with watchlist count and play button, and "Done for tonight" (§2.2 A–B). | `swipe.js` |
| 2.5 | **Card hook**: one anchor title, a one-line pitch ≤ 140 characters, mood pills (§2.2 C). | `swipe.py:141-193`, `swipe.js:356-412` |
| 2.6 | **Calibration deck** for a member without a taste profile, with the first taste build after 5 seen answers (§2.2 D, §2.1 E). | `swipe.py`, `routers/swipe.py`, `swipe.js:264` |
| 2.7 | **Variety re-rank** of the neighbour pool (§2.2 E). | `swipe.py:214` |
| 2.8 | **Loved → visible effect**: re-rank the cached library lane, show a pill (§2.2 F). Undo removes the watchlist entry (S7). | `swipe.py`, `plex_watchlist.py`, `jellyfin_writes.py` |
| 2.9 | **`GET /api/home/tonight`** with the Tonight pick rules (§2.1 A) and a post-watch verdict store (§2.5). This replaces 1.6's chat-routed answer with a direct one-tap write and the "moved up" echo. | new `routers/home.py`, `services/tonight.py`, migration |

### Stage 3 — Wrap-Up storytelling and micro-moments (about 1–2 weeks)

| # | Change | Files |
|---|---|---|
| 3.1 | **Period keys `week:` and `month:`** in the member's zone. | `wrapup_facts.py:98-118`, `wrapup.js:16` |
| 3.2 | **New narrative facts**: ritual, comfort genre, of the month, night shift, first of the period (§2.4 B), offered to the editor as cards. | `wrapup_facts.py`, `wrapup_insights.py`, `wrapup_writer.py` |
| 3.3 | **"Your week" card** (deterministic) on the Tonight header, Sunday evening to Monday. | `services/tonight.py`, `wrapup_sections.py` |
| 3.4 | **Monthly mini-story** pre-written by `wrapup_pregen` on the 1st, with a "Your month is ready" card. Year card from 1 December. | `wrapup_story.py:440-469`, `data_custodian.py:887-888` |
| 3.5 | **Playlist deep link** plus a play card; the Jellyfin skip handled; one-tap week and month playlists (§2.4 C). | `plex_playlists.py:712-787`, `jellyfin_writes.py:163-197`, `routers/wrapup.py:305-342`, `wrapup.js:505-508` |
| 3.6 | **Per-slide share images** from the existing `share:` strings (§2.4 D). | `wrapup_story.js` |
| 3.7 | **Warmer fallback templates** (§2.4 E). | `wrapup_writer.py:51-54` |
| 3.8 | **UI language follows the member** (H8): start with the landing, Tonight, Swipe and Wrap-Up strings, the surfaces a member touches. This is the ROADMAP "German locale" item, scoped to the living room first. | `frontend/js/*`, new strings module |

---

## 4. How we will know it works

These metrics are for the owner only, computed from existing tables, never
shown to members and never used to nudge:

- **Return rate:** members who open Curatarr on ≥ 2 different days per week
  (`User.last_login` plus a per-day "opened" mark; add one if the session
  table cannot answer it).
- **Time to first action:** seconds from landing to the first play, watchlist
  add or swipe answer.
- **Follow-up answer rate:** answered post-watch cards ÷ shown. Compare the
  bell-only baseline with the Home card.
- **Chat turn depth:** the median number of user turns per free-chat thread.
  The conversation-shape rule should raise it.
- **Deck completion:** decks finished ÷ decks started. Watch the share of
  "Done for tonight" against abandoned decks.
- **TV pull:** plays from Curatarr Recommended or the watchlist per member per
  week (`_process_rec_watch_hits`).

When a proposal makes a member feel graded, hurried or watched, it is wrong,
whatever the metric says.
