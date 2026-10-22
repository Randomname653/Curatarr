# Changelog

Condensed release history, newest first.

---

## Unreleased — a deletion judge that fails closed, the AppSec review, one SQLite writer

**Light instead of lines: a calmer, more tactile interface.**
- Panels, cards and dialogs are lit from above (a hairline edge, a faint top highlight,
  soft layered shadows) instead of outlined; the topbar and sidebar are frosted glass,
  and the canvas carries a faint amber light. One type scale and one spring curve.
- Posters shimmer until they arrive and fade in; a failed one stays a quiet placeholder.
  A card picks up its poster's colour as a soft ambient light, stronger on hover.
- Deletion cards lead with the curator's pitch; the size reads "frees 4.2 GB" (or MB/TB),
  Keep / Discuss / More rest a step back until the card is hovered, and an empty note
  is one dashed line until you use it.
- Buttons, chips and tabs give a little when pressed on every device, and switching
  views crossfades where the browser supports View Transitions.
- Chat: your own messages are a warm tint instead of a solid amber block, replies keep a
  readable line length, the input floats as a glass bar, and a reply growing below the
  fold no longer yanks you down while you reread: a "New reply" pill offers the way.
- Phones: the tab bar floats as a glass dock, sheets can be dragged down to close, and
  the tables in Activity, History, Knowledge Base, Libraries, Settings, Users and setup
  turn into stacked rows instead of scrolling sideways.
- Settings: on/off options are switches, the vibration level is a segmented control,
  and vibration now also marks sends, Why? toggles, destructive confirms and errors.
- Fixes after the first look: tables are full width again on desktop (their phone
  class collided with an existing layout utility), chat sits in one centred column on
  wide screens, Reclassify uses the same sections as every other view, and every
  checkbox is amber.
- Music artists in the library show their Deezer picture again (from the SoulSync
  profile's Deezer id; the Plex music index has no images since Lidarr went off).

**Curatarr on your phone: an installable app with a real mobile layout.**
- Installable (PWA): a manifest, home-screen icons and a service worker. Open Curatarr
  over https (or on localhost) and the browser offers "Install app"; on iPhone the
  drawer's Install button shows the Add-to-Home-Screen steps. The app shell opens when
  the server is out of reach; API answers are never cached.
- Below 768px a bottom tab bar replaces the hunt for the hamburger: Chat, For you
  (Recommendations), Swipe, Library (a sheet with TV Shows / Movies / Music) and More
  (the full drawer, with a dot when something there needs you).
- Gestures: swipe in from the left edge to open the drawer, pull down at the top of a
  view to refresh it, tap the active tab to scroll back to the top.
- Swipe on a phone fills the screen between the topbar and the tab bar: the card and
  all six reactions fit without scrolling, the exact-star row is a 5×2 grid of big
  buttons, and every decision gives a short vibration (Android; a double pulse for
  Loved, off with reduced motion). Fix: the reaction buttons, the exact stars and the
  glow around a dragged card now show their reaction colour; a default colour later in
  the stylesheet had overridden it.
- Dialogs open as bottom sheets, toasts sit above the tab bar, tabs scroll sideways,
  Settings' sub-navigation becomes a chip row, the chat input grows with your text and
  uses icon buttons, and every edge is padded for the notch and home indicator.
- The notification count appears on the installed app's icon, and the topbar says
  "offline" while the phone has no network.

**Chat no longer waits on background work or on reloading the model.**
- A chat message goes ahead of every queued background job (recommendations, proactive
  messages, chat openers, principle learning). If one of those is already generating, it
  is stopped and re-run after your reply.
- Chat no longer loads the small model before your reply. On a 24 GB card that pushed
  the curator out of VRAM, and the reply paid for a full reload. The title in your
  message is now read by the curator itself. The Wikipedia/reception summaries are
  fetched in the background once you are idle and are there from the next message on.
- Work after a reply (keep/protect detection, memory extraction, verification answers,
  bulk enrichment) waits until you have been quiet for a while. It no longer pushes the
  curator out between your reply and your follow-up.
- Keep/protect detection only runs when your message contains a keep phrase (or a bare
  "yes" in a discussion about a title). Memory extraction is skipped for conversations
  with no likes, dislikes or reasons in them.
- The curator stays loaded for 5 minutes after a reply unless background work needs the
  card. It used to be unloaded after 60 seconds, so reading a long reply cost a reload.
- Every curator request uses the same context size and keep-alive. The deletion judge and
  the taste summary used to shorten the curator's keep-alive. The taste summary also
  reloaded the small model at the curator's 16k context.
- During a deletion scan using the separate pitcher model, the scan waits for your
  conversation to settle before taking the GPU back. Each message used to swap both
  models out and back in.
- Focusing the chat box or typing starts loading the curator if it is not already in
  VRAM, so it loads while you type instead of after you press Send.

**Faster deletion scans and taste rebuilds.**
- The judge's per-title watch lookups use new watch-history indexes: under a millisecond
  instead of 30–180 ms on a 400k-play history.
- Learned keep-considerations for the 60 judged candidates come from one batched
  embedding call and one read of your memories. That was a call and a full read per
  candidate, and the scoring is now about 34× faster.
- While the GPU writes one verdict, the next title's evidence is already being gathered.
- Reception, dialogue and Wikidata lookups before a scan run a few titles at a time,
  alongside the significance pass, instead of one after another.
- A taste rebuild pulls each category's vector corpus once (4–9 s for 30k vectors), not
  twice per household member.
- Pages and API responses over 1 KB are gzip-compressed. Live streams are not.

**Months without a restart: bounded memory, retention, and healing after a restore.**
- Search's in-memory phrase vectors are capped (about 100 MB at most). They used to grow
  with every search for as long as the app ran.
- A database restored without its vector store, or with an older one, is noticed at
  startup and daily. Missing vectors are rebuilt from the cached profiles in the
  background, titles whose profile is gone too are enriched again, and the owner is told
  once a week while the two disagree.
- A deletion whose outcome was unknown ("limbo") is recorded as deleted once Radarr,
  Sonarr, Lidarr or Plex confirm the item is gone. One still in the library stays for you
  to retry, with a weekly reminder. A delete stuck out of sight after a failed save shows
  up in limbo the next day instead of after the next restart.
- The weekly database clean-up (VACUUM) runs on servers that are never restarted, and
  covers the lyrics index too.
- Old retired rows are removed: deletion proposals replaced over 90 days ago, read
  notifications over 90 days old and used conversation starters after 30 days. A row
  someone discussed in chat is kept.
- `update_db.py` keeps its newest three pre-migration copies instead of all of them. The
  daily backup is skipped, loudly, when the disk could not hold it plus as much again.
- Memories and principles that were replaced keep their text but drop their stored
  vectors, and saving a new memory no longer reads every replaced one.

**Easier to use with a keyboard and a screen reader, and recommendations say why.**
- Chat replies are announced once they're finished, not word by word.
- Keyboard focus stays inside an open dialog.
- The tab you're on in Deletions, the Knowledge Base, the arr pages and Recommendations
  is part of the link, so a reload or a bookmark opens the same tab.
- Recommendation cards have a "Why?" that lists what each pick was based on: how close it
  is to your taste, the genres you like, trending, your own stated values, and so on.
- The thinking-status text is readable now, and an armed delete button is solid red.
- New optional setting `ALLOWED_HOSTS`. Set it to the names you reach Curatarr by, and
  requests for any other name are refused, which blocks DNS-rebinding pages. It is empty
  by default, so nothing changes until you set it.
- A music job that crashed can no longer leave the music pipeline marked as running.

**Household members' data stays theirs, and maintenance stops repeating itself.**
- A household member who tells the curator to keep a title is heard: the deletion judge
  can't cut it any more, it becomes the owner's call. This holds with the pillar judge
  switched off as well.
- A title deleted and added back in Radarr/Sonarr/Lidarr no longer shows up twice in
  search. Its old vector is merged into the new entry: kept, but hidden from search,
  and no longer rebuilt. The chat now says "protected for
  you" to members rather than "permanently protected".
- Orphan Repair credits each play to the Plex account that watched it. Before, every
  household play went to the admin's taste profile.
- The owner's SoulSync follows and likes only shape the owner's music taste.
- Principle capture from deletion debates runs for the owner only.
- Re-filing a show as anime (or back) takes its protections along.
- The nightly SoulSync→Lidarr catalog sync no longer re-adds artists you deleted.
- The SoulSync ban of a deleted Lidarr artist uses Lidarr's own MusicBrainz id. A Retry
  whose item is already gone counts as deleted instead of ending in an error.
- A reply like "which show?" in a verification question's thread no longer counts as
  your answer; the question stays open.
- A maintenance task that fails waits longer before each retry (up to its normal
  interval) instead of rerunning every 30 minutes.
- The weekly audit no longer rebuilds the same orphaned vectors every week.
- The OMDb top-up keeps going until its backlog is empty, and anime rows are no longer
  fetched again every day.
- Two overlapping runs can no longer fill the notification bell with duplicate curator
  messages, and the hourly message cap is now applied. Verification questions are asked
  once, even when two jobs start at the same moment, and they expire after a week.
- Filling in missing external ids skips titles whose match you pinned.
- Music lookups treat "Foo" and "foo" as the same name, and a name whose lookup keeps
  failing is parked like one that was not found.
- Re-filing a show also removes its profile under the old category. If the weekly audit
  fails partway through a requeue, the finding keeps its one automatic retry.
- The anime mapping cache keeps every season's AniDB entry.
- The Spotify backlog and the wish list mark artists you deleted.
- Household members can wish for music too (when music runs through Plex without
  Lidarr): a **Wish** button on music recommendations and in their Spotify backlog. The
  wanted list shows who asked. The owner removes any wish, a member takes back their
  own, and members have a small hourly budget and a cap on open wishes.
- Each person's Recommendations page shows when their own recommendations were built.

**Members can't overload the box or see the admin's network.**
- Rebuilding a taste profile, forcing a live arr refresh and re-fetching metadata from chat
  now have small per-user budgets. Only one taste rebuild per user runs at a time.
- An admin can no longer deactivate themselves or the last active admin. Before, that
  locked everyone out of the admin side until someone edited the database.
- Members no longer get root folders, Plex library paths, arr addresses or raw error
  text. The detail goes to the server log.
- The model-update review shows judgements the judge didn't answer, and says "no verdict"
  when most of them failed.
- A taste rebuild where most embeddings fail now keeps the old profile and says so,
  instead of quietly writing a skewed one.
- Large Spotify histories no longer freeze the app for a few seconds during the Last.fm
  genre top-up.

**When something can't be read, Curatarr now says so.** Several checks used to read an
unreadable value as "all clear". Now:
- an unreadable game flag counts as a game;
- a server-address check whose name lookup times out is refused;
- an unreadable OMDb usage count leaves no budget for the day;
- the music pipeline's Stop button works even when its flag can't be read.

Screens that showed a failure as if it were real data now say what couldn't be loaded:
- Sonarr/Radarr history on the deletions page;
- enrichment coverage;
- arr profiles in library settings;
- chat history.

A corrupt taste profile is flagged on the deletion run instead of being quietly
ignored. The enrichment status counts are no longer shown to another user for a few
seconds. Two people logging in for the first time at the same moment can no longer
both become admin.

**Fewer pauses from game checks, web requests and large imports.** The "is a game
running?" check (`nvidia-smi` plus a scan of every process) ran before every AI request
and for every enriched or judged title. It now answers from the last result, refreshed in
the background every 30 seconds. Every web request also loaded the system's certificate
list again (about 50 ms each); that now happens once at startup. Large Radarr, Sonarr and
Lidarr libraries, the Spotify import, the anime database and the image cache are read
and written off the thread that serves pages. The test run now fails if any code makes
that thread wait on the database.

**Searches and enrichment no longer pause the server.** Vector-store calls (search,
recommendations, the facet index, every enriched title's write, the embedding
migration) now run on their own thread instead of the one that serves every page. The
vector store opens once at startup instead of on the first search. The monthly Discogs
import unpacks and reads its multi-gigabyte download off that thread too.

**Every database file off the event loop; foreign keys enforced.** The lyrics, cache
and offline-index files are now watched by the same check as the main database. That
turned up the lyrics sync, which held its file locked across every Plex request; it now
holds the lock only while writing. The enrichment cache waits 15 seconds for another
writer instead of 60. The database now enforces that every row belongs to an existing
user. At startup, rows from the past that name a missing user are repaired: a link that
can be empty is cleared, and anything else is kept in `fk_orphans` before removal.

**The server no longer waits on the database.** Every page, stream and background job
shares one event loop, and 561 database calls ran on it directly: while another program
(the music enricher, a backup) held the database, the whole app could freeze for up to a
minute. Those calls now run on worker threads. A built-in check logs any new one (and lists
them at `GET /api/system/loop-guard`, admin only), and a test stops them coming back. The
database now gives up waiting after 15 seconds instead of 60. Two crashes this turned
up are fixed: the Deletions list failed when served from its cache, and proactive
messages were never generated.

**No silent error handlers left.** The last 277 handlers that swallowed an error
(`pass`, or a DEBUG line nobody sees) now each do one of four things: narrow to the
error they expect, log a real failure as a warning, keep outages quiet while bugs are
logged, or say in the code why staying silent is safe. Where one guarded a decision it
now fails closed:
- a half-broken embedding migration can no longer pass its own quality gate;
- the deletion judge defers a title when the owner's taste profile or its format check
  can't be read;
- a corrupt notification setting no longer switches every opt-out back on;
- OpenSubtitles pauses when a download can't be counted against the daily budget;
- background learning steps back when it can't check whether a chat needs the GPU.

A test now refuses any new silent handler that doesn't give its reason.

**The deletion judge fails closed.** When the judge could not read the
watch history, its facts said "not watched", a made-up negative for the
household pillar; such a title is now deferred. Verdicts are checked
against a schema (all four pillar findings, a verdict that agrees with the
pillar it names) and a malformed answer gets one repair turn; a second
failure leaves the title undecided instead of proposing it. Markup is
stripped from the parsed answer, no longer from the raw text, where a `<`
in one finding and a `>` in the next deleted everything between them. A
CUT on a title another household member has watched or played becomes
Your call, whatever the model said. The run stops when a game starts or
after two outages in a row, and the Deletions banner names why titles
could not be judged instead of "the model didn't answer".

**Prompts fit the window on purpose.** The judge's evidence is assembled in
priority order and trimmed to the room its system prompt leaves (the title,
owner and household lines are never cut); an oversized prompt is refused
before the call instead of Ollama cutting the constitution off, and chat
drops its oldest turns to fit. `LLM_PROFILE=small` uses an 8k window and
gives the judge a compact checklist version of the constitution;
`CURATOR_NUM_CTX` and `CURATOR_NUM_GPU` set the window and the GPU layers
directly. The defaults are unchanged (16k, every layer on the GPU). The
baked models carry the window the calls ask for, so a call that leaves it
out no longer reloads the model; run `build_models.py` once to pick that up.

**The AppSec review of 2026-10-10** (`docs/security/appsec-audit-2026-10-10.md`).
The first-run exemption for a local browser now wants a browser that
addresses Curatarr as localhost: a DNS-rebound page or a reverse proxy on
the same machine needs the setup code. "Test connection" sends a saved Plex
token or arr key only to the saved address, so a new address needs its own
key, and service URLs must be plain http(s) base URLs (no credentials, query
or fragment, no link-local or cloud-metadata address). A failed test answers
with a classified sentence and the HTTP status at most: never the page it
got back, never refused-versus-timeout (a port scanner's question), never
exception text (CodeQL). A delete with a malformed arr id or Plex rating key
is refused before any request; the image proxy serves raster images only,
under a sandbox CSP; the arr profiles endpoint is admin-only. The encryption
passphrase (PIN) is gone: it encrypted nothing, and its stored hashes are
dropped at startup with secure_delete.

**One writer for SQLite** (`docs/SQLITE_ASYNC_CONCURRENCY_AUDIT.md`). A fresh
install lost four columns of the deletion proposals table on its first
start, and every Deletions query failed until the next restart; the table
rebuild now creates the table from the model and keeps every column both
versions share. The music matcher held SQLite's write lock across an await,
so a writer on the same loop (the game watcher) could freeze the app for
the 60-second busy timeout; it now matches in memory and commits each chunk
in a short write session (BEGIN IMMEDIATE) on a dedicated writer thread.
Shutdown cancels background tasks with a time limit, drains the writer and
checkpoints the WAL before Syncthing gets `data/` back. Crash cleanup on
Linux and macOS wrote to the wrong database file (the path lost its leading
slash).

**Less waiting on the event loop** (`docs/POST_REFACTOR_INTEGRATION_AUDIT.md`).
The metadata cache keeps one connection per thread instead of opening a new
one, with its PRAGMAs and schema check, for every lookup; the deletion
scorer reads all candidates' ratings in one query, and a cache handle
dropped on an error no longer leaves its transaction open. Deletion
scoring, the taste rebuild and memory-conflict checks do their database
reads, ChromaDB reads and NumPy work on worker threads, so the UI and the
Activity stream stay live while they run. The game watcher's `nvidia-smi`,
process scan and Ollama check run off the loop, and it writes its state
only when a value changes (it wrote `game_active` every 30 seconds).
`set_state` is one upsert statement: two first writers of the same key no
longer fail on the primary key. A chat reply no longer holds a database
connection while it streams. Column migrations run in one transaction and
stop the start with an error if a step fails, instead of logging it at
DEBUG; `update_db.py` refuses to run while Curatarr is up and backs the
database up first. A "Your call" card that the household rule produced
says so in its tooltip.

**Failures that say so** (`docs/SILENT_FAILURE_SWEEP_2026_10.md`). A sweep of the
code's error handlers, after the taste-cluster read that failed silently for months, found
a second one like it. The memory extractor read its thread's subject after the database
session had closed, so no discussion thread got one, and a verdict from a "how was it?"
thread never reached the taste profile. Deletions now stop instead of judging without a
protection they could not read: a Keep cooldown, the curator's stances, recent plays,
Plex star ratings, listening depth or the learned keep-reasons. An artist whose listening
history could not be read is no longer "never played"; it is deferred. Game mode no longer
switches off when a status probe or the flag write fails, and an unreadable game flag
counts as a game. Each stale lock is cleared at start on its own. Service addresses
spelled as a decimal, hex or IPv6-mapped number no longer get past the link-local check.
A Discogs download cut short keeps the complete styles database, and a MusicBrainz outage
no longer parks artists as "no match". In the browser, badge, banner and hover colours
are back (four colour tokens referred to themselves); a bulk delete whose progress can't
be read says so; a failed start-up check no longer opens the Setup wizard.

**…and the follow-up.** Signing out on another device, or a deactivated account, now
ends the session in every open tab at once, with the reason on the sign-in screen. Before,
the views showed a Retry that could never work, and the bell and Activity kept failing in
the background. Activity says when its live stream is down and retries with growing gaps.
The deletion judge defers a title when the owner's feedback, keep-reasons, principles or
verified facts could not be read. A Last.fm, OpenSubtitles or Plex outage, a spent
subtitle quota or a rejected key is retried, not recorded as "no genres" or "no subtitles"
for good. A profile whose vector never reached the vector store is embedded again on the
next pass. A failed save after a delete no longer fails the rest of a bulk delete, and a
Plex artist delete that can't be confirmed is listed as unconfirmed rather than failed or
done. "Test connection" refuses a host name that resolves to a cloud-metadata address,
Spotify uploads are bounded, and sign-in errors no longer echo library or plex.tv text.
Re-attributing history to household accounts runs off the event loop in short writes;
a large Plex sync commits as it goes; the weekly VACUUM waits for long jobs and skips files
with little to reclaim; and a music enricher running outside the server keeps its claim
across a server restart. A new test keeps the number of silent error handlers from
growing.

**…and the rest of the list.** 80 endpoints that only read or write the database
now run off the event loop, so a slow query no longer freezes the UI and its live streams.
Failing titles go to the back of the enrichment walkers' queue. Setup refuses an address
that resolves to a cloud-metadata host. A failed read no longer turns into a confident
answer:
- the vector store reports dimension mismatches;
- an unreadable memory flush stops "+ New" from deleting the chat (you can still clear anyway);
- a rebake that failed is not shown as an installed model update;
- a GPU check that times out counts as busy;
- an empty download never replaces the anime mapping;
- a Plex outage keeps playlist and collection pushes due instead of deleting the shelves.

The views say when a request failed instead of showing stale or empty state, deleting
explains a lost reason, and "Why?" closes with Escape.

**Card tags that tell titles apart.** A deletion card showed the arr's first
four genres, so nearly every anime read "Action, Animation, Anime, Drama".
Cards now take the genres from the enriched profile (AniList's for anime:
Ecchi, Mecha, Slice of Life), drop what the tab already says, put the
genre that is rarest in your library first, and add up to two short
themes in a quieter chip ("Anthropomorphic warships", "Drug heist"). Music
cards, which showed none, get theirs from the artist profile.

**The deletion scan reads ratings safely.** One profile whose rating read
"N/A/10 (N/A votes)" stopped every scheduled scan: the summariser had echoed
its prompt line, and Jikan/MAL put the content rating ("PG-13 - Teens 13 or
older") in the same field. Ratings are now read as a number or not at all,
profiles store the source's score instead of the model's echo, and the
prompt says "unknown" when there is none. Profiles already holding text
are picked up by the audit's re-enrichment.

**Dependencies.** aiohttp 3.14.5 for GHSA-2g87-pp6c-29x5, GHSA-5wr8-gcqf-gqf3
and GHSA-494j-54xm-rwp3 (fixed in 3.14.4). Curatarr uses aiohttp only as the
client for the arrs, never as a server, so the request-timeout and request
flooding findings did not reach it; the update keeps the dependency scan green.
Pillow 13.0.0 fixes 38 advisories in 12.3.0. Curatarr only draws the tray
icon with it, which renders unchanged. certifi moves to 2026.10.19, and the
OpenTelemetry family (via ChromaDB) to 1.45.1, all for their advisories.

**A household member's plays reach watch history.** The Plex sync listed
watched items with the admin's token, and Plex answers "watched" for the
token's account: a title only a member watched never showed up, so none of
their plays reached Curatarr and the household rule could not protect what
only they watch (a 14-episode show was proposed as a Cut). Members' plays
now come from their own Plex play history - the whole history on the first
sync, incrementally after that; accounts without a Curatarr login are never
filed under the owner.

**Swipe.** A new view walks the library titles closest to your taste that
you have not played in Plex, one card at a time: "Not seen" moves on,
"Seen it" asks for 1-10 stars. Seen-and-rated titles join your taste
profile (10 stars pull it toward the title, 1 star pushes it away), so
what you watched on Netflix or in the cinema finally counts. Every ten
answers rebuild that category's profile in the background. Keyboard
(N, S, 1-9/0, U to undo) and drag gestures on phones.

**Swipe, the real thing.** The view now uses the whole screen: a large
card (poster at two thirds of its height, facts beside it) on a deck of the
next two, a blurred copy of the poster behind everything, your progress on
the left and what you just rated on the right. Five reactions replace the
row of ten numbers - Not seen, Disliked, Meh, Liked, Loved (stars 2 / 5 / 7
/ 10 in the same store, so the taste weights are unchanged); exact stars
are still one key (1-0) or the Exact button away. Drag in four directions
(← not seen, → liked, ↑ loved, ↓ disliked) or flick: the card tilts from
where you grabbed it, a stamp and a glow grow toward the decision, phones
buzz at the point of no return, and the card flies off the way you threw
it while the next one rises. Buttons and keys play the same motion.
- Each card says why it is there: the taste cluster it sits nearest ("Near
  your taste for Heat, Ronin"), the genres and themes your profile favours
  marked in amber, and the enriched profile's facts - runtime or seasons,
  content rating, score, Rotten Tomatoes, director, cast, mood.
- Loving a title pulls its nearest unanswered neighbours to the front of the
  stack ("Because you loved ...").
- Undo flies the card back in from where it left instead of reloading the
  stack; a ring counts the answers to the next taste refresh and turns
  green when it starts.

**Swipe: Want to watch.** Most cards are titles you haven't seen, and "Not
seen" threw away whether one interested you. "Want to watch" now sits next
to it and puts the title on your own Plex watchlist (the same write chat
makes for "I want to watch that"; the card says if plex.tv found no clear
match). It is a plan, not a verdict, so it doesn't touch your taste profile.
The two unseen answers get the two easy swipes (← not seen, → want to
watch; ↑ loved and ↓ disliked as before; Liked is a tap or L). Exact stars
moved off the button row to a long press on a seen reaction (or keys 1-0),
so the row stays at six. Only seen answers count toward the next taste
refresh, and a full stack loads its posters in one round.

**Vibration you can turn down.** Settings → Account → This device:
Off, Light (the new default: one pulse at half length) or Full. Saved in
this browser only; Android phones, iPhones don't let web apps vibrate.
The swipe card also drags from its text now, not only from the poster.

**The curator in the middle.** The phone tab bar is For you · Swipe ·
Chat · Deletions (members: History) · More, with Chat as the raised
button; Swipe is an ordinary tab now. Library and Report moved into the
drawer, and the drawer has one way in: More (or a swipe from the left
edge) - the second menu button at the top left is gone.

**A library you can browse.** TV Shows, Movies and Music are a poster
grid now, on the desktop as on the phone: title, year and size under each
poster, a small amber dot where the synopsis is missing or still raw.
Tapping a title opens its details - synopsis, size, added, status, and for
admins the root folder and Re-enrich (Metadata / Summary / Both), which
used to sit as a menu on every row. Search comes first in the toolbar.

**Fewer random memories on deletion cards.** "Matches something you said
you value" no longer shows a memory that only stood out of a tight cluster
of unrelated ones (a car-nostalgia note on a French comedy). An
embedding-only match now needs a cosine of at least 0.34. That is just
above the measured noise, and real matches sit at 0.35 and up. Matches
on a shared word work as before.

**Cloudflare Access at the origin.** Publishing Curatarr under a public
name through Cloudflare and a reverse proxy no longer depends on nobody
finding the proxy directly. Requests under a `CF_ACCESS_HOSTS` name must
carry a valid Cloudflare Access token (RS256, the team's keys, the app's
audience), or they get a 403. Without team or audience set, those names
refuse everything. The LAN address is never checked, so local use never
depends on Cloudflare. A Plex login started on the public name returns
there.

**Sign-in on phones, and slow first tokens.** After a Plex sign-in, the
tab plex.tv sends back no longer shows the login again. It reloads
signed in as soon as the other tab finishes. A chat reply whose first
token takes longer than 45 s (model load, long prompt) is no longer
dropped as "the connection went quiet": the keepalive now also runs
while Ollama has not answered at all.

**Container image.** `ghcr.io/randomname653/curatarr` is built from the
hash-checked lock on a digest-pinned Python 3.12 base. It runs as a non-root
user and keeps everything, the `.env` included, in one `/app/data` volume.
CI builds and starts it on every PR, and every push to `main` publishes it.
`deploy/docker-compose.yml` is ready for a TrueNAS custom app. Ollama stays
outside (`OLLAMA_ENDPOINT`). `CURATARR_ENV_FILE` moves the `.env` on any
install.

**The gaming PC reports itself.** With Curatarr on a NAS, the PC's tray
runs a small agent (`PC_AGENT_TOKEN`) that reports its programs and GPU, so
game detection keeps protecting the PC. The NAS polls it every 5 s. A PC
that shuts down says so at once, and one that stops answering for two polls
counts as gone. No work is sent to it then, and no eviction is attempted on
it.

**Two machines.** `OLLAMA_PC_ENDPOINT` adds the PC's card as a second
host. While the PC is on and free, chat, background curator work and
enrichment run there with the PC's own profile. Otherwise they run on the
main host (the NAS, small profile). Deletion runs wait for the PC instead
of judging with the small model, and embeddings stay on the main host. A
game on the PC takes it out within seconds and evicts Curatarr's models
from its card. A call on the PC ends within a second of it leaving (a
shutdown, a power cut or a game) instead of hanging until a 5-10 minute
timeout:
- a verdict counts as an outage and decides nothing
- an enrichment item stays due
- a chat reply that has not started is answered on the NAS, one that has
  started ends with a note and is not saved

While the PC is free, a deletion run goes three times deeper, so the
proposals last while it is off.

**Deletion verdicts are not paid for twice.** A verdict is stored under a
fingerprint of everything that decides it: the evidence, the principles,
the bake and its weights, the host's profile. The next run reuses it while
nothing changed, does not count it against its cap, and reaches further
down the list. Outages are never stored. Pitches are kept per language.

**Docs.** README and USAGE walk through a reproducible first start (the
prerequisites, the Windows install in three steps and the line that proves
Curatarr is up), troubleshooting is named by symptom (LAN access, the Plex
redirect, Ollama down, port 8000), USAGE lists the small-GPU settings, and
CONTRIBUTING holds the lock workflow and a dev setup that starts.


## 2026-10-10 — v1.1.0-beta: enrichment that heals itself, deletions that explain themselves

Five weeks after 1.0.1: the enrichment pipeline heals itself and says what
it is waiting for; deletions explain themselves (Cut or Your call with a
Why?, Keep and Delete dialogs that say what they do, deleted titles that
come back are shown); views can be linked, bookmarked and read at accessible
contrast; MyAnimeList's official API replaces Jikan; SoulSync's follows and
likes shape music taste and its blocklist keeps deleted artists gone; and
the custodian keeps the GPU busy while you are away. In detail:

The Knowledge Base said "93 % enriched" while the libraries reported 46 to
92; the "Not findable" column was always zero because it read a column
nothing wrote; a fresh miss counted as enriched for three days; a title no
source knew was re-fetched every three days for ever; an API outage looked
like hundreds of unfindable titles; and the weekly audit purged and
re-fetched the same wrong-entity profiles every week without anyone seeing
it. SoulSync's healing mechanics (MIT) were studied and the portable parts
ported.

**One honest state model.** One classifier for the KB tile, the breakdown
endpoint and the producer, with the definitions served to the UI. States:
enriched (incl. provisional), dead cache, rule-based, awaiting LLM, retry
due, not found (waiting), queued, processing error, ignored, never
processed. The Glance tile and the watch-history table no longer count
sentinels as enriched.

**Retries that back off instead of hammering.** Not-found rows carry an
attempt counter: two quick tries, then 3, 6, 12, 24 days, then monthly —
for ever (nothing is given up on). Existing rows are backfilled once on
upgrade, dated from their own last write. An upstream outage (429/5xx on
any source) is recorded as *unavailable*, keeps the item due and writes
nothing; a category that aborts on the outage guard gets its attempts back.

**Why not 100 %, per item.** Every count in the KB table opens the list
behind it, each item with its attempts, next retry, per-source outcomes
(TMDB ✗, OMDb not asked — no IMDb id, …) and a one-sentence reason. A
**Needs attention** panel (badge in the nav) lists what the pipeline
cannot settle alone: found under the wrong year, refused as too far off,
matched with middling confidence, not found after two or more tries,
audit findings (wrong entity, shared id, pin contradicted), or no external
id in the arr at all.

**Owner tools.** Search & pin from the arr's own lookup (tvdb / tmdb / imdb /
mbid), TMDB or AniList, or a typed id of any kind — from the KB page and
from deletion cards, category resolved server-side. "Not this one" excludes
a wrong candidate for good (all four title-search resolvers skip it). Retry
now, Ignore / Un-ignore (an accepted gap leaves the open count), Dismiss
finding.

**Match confidence.** Title-search resolutions store which authority
resolved the entity and a similarity score; middling matches are accepted
but flagged, far-off ones with a disagreeing year are refused instead of
stored as a wrong profile.

**Findings inbox.** The audit's verdicts persist as rows with a lifecycle
(pending refreshed in place, dismissed silent, resolved re-raised after a
week if still there). Each finding gets exactly one automatic re-resolution;
a second detection escalates it to a human. Pinned items are skipped by the
entity check unless the profile contradicts the pin.

Also: the LLM-failure path crashed the status writer, so "Processing
failed" was never recorded; fixed. Migration is automatic (new columns, a
new table, one backfill).

### Security, second angle: what a household member can make the server do

The first pass asked who can call what. This one asked what a logged-in
*member* (or a stolen member token) can make the server spend, what text
from the outside world reaches the models, and where an external body
touches memory or disk. Verified with a probe instance and a minted member
token before and after.

- **Per-user budgets on the endpoints that turn one request into GPU or
  API work.** Chat: 30 messages per 5 minutes and one reply in flight per
  user (a second concurrent request gets 409, the guard self-expires so a
  dropped stream cannot lock anyone out). Semantic search 20/min, arr
  lookups 30/min, recommendation generation 2 per 10 minutes and one at a
  time — including the `GET /api/recommendations/?refresh=…` path that
  bypassed the refresh endpoint's guard — music pipeline starts, sync
  triggers, memory flushes. The global curator gate serialised the GPU; it
  never stopped one caller from owning the queue.
- **One Plex sync at a time.** The hourly cooldown was read from a stamp
  written only when a sync *finished*, so triggers inside one window ran
  full syncs side by side. Same state lock as enrichment and the music run.
- **Bounded bodies.** OpenSubtitles downloads are capped while streaming
  (4 MB, the provider leg already did this) and the CPU-bound subtitle
  metrics moved off the event loop — an oversized file used to pin the
  whole app for every user. The image proxy enforces its 5 MB cap while
  reading instead of after buffering. Spotify zip members are read through
  a bound (50 MB each, 400 MB per archive) and the import directory is
  anchored to the project root instead of the process CWD.
- **Third-party text is data, not instructions.** Overviews, reviews,
  Wikipedia extracts, bios and the shared verified-data block are fenced
  with explicit markers and scrubbed of markup, chat special tokens and
  role markers before they reach a model; every system prompt that receives
  fenced text carries the rule. Studio and director notes cache for a year
  instead of a decade. Model output is stripped of tag-like markup before
  persistence; Plex shelf names and blurbs are plain printable text.
- Request shapes: chat messages max 8 000 characters, music pipeline
  batch ≤ 2 000, list sizes capped on the recommendation and library
  endpoints.

### UI grammar — one visual language down to the deepest view

The healing UI "worked but looked rudimentary; the deeper you dig, the more
ad hoc the buttons get". Three inventories agreed: five recipes for "a row
with actions", three tab systems, four ways to confirm or report (native
alert/confirm/prompt, hand-built countdown overlays, button-text mutation,
and a `showToast` that was called but never defined — the coverage banner's
"Enrich library now" silently failed for months), some 830 inline styles.
Patterns were taken from UIs that are genuinely usable — Sonarr's toolbar
and mass-editor footer, Plex's Fix Match dialog and three-actions-plus-
ellipsis, Jellyfin's progress-on-the-row, SoulSync's bulk bar that appears
only with a selection (MIT) — and applied as one grammar; Curatarr keeps its
own look.

**Foundation.** One toast, one modal, one confirm dialog (the 3-second
countdown and the reason field kept), one "More" menu, one pager, one empty
state, one inline status, one dirty-form guard; `.toolbar`, `.section`,
`.select-bar`, `.banner`, `.chip`, utilities. Dead CSS removed.

**Knowledge Base.** Five tabs (Overview · Needs attention · Maintenance ·
Music pipeline · Profile browser) instead of nine stacked panels; every
number is a link to its list, the list is a section with the state
explainer, a pager and Close; Needs attention filters by reason (server-side
`reason=`); rows carry Search & pin · Retry now · Ignore with Open-in-arr
behind More; the match picker is a dialog shared with the deletion cards.
Two faults the seeded probe exposed: the page returned 500 on the first
pending audit finding (a session closed too early), and the sidebar badge
counted by attempts alone while the page used the full reason set — both
share one rule now.

**Deletions, Curation, Recommendations, Report.** Toolbar + selection bar;
cards with Delete · Keep · Discuss visible and Reevaluate, Fix match, Open
in arr behind More; the note saves itself; Keep uses the note as its
reason; bulk results as toasts. Curation sections with real empty states and
confirmations; recommendation lanes as chips; the report in sections.

**Activity, Admin, Settings, Libraries, arr browser, Reclassify.** Task
rows with the status badge and Cancel at the row end; a table for the
history; Disable behind a confirmation; Settings as sections whose Save
enables once a field changed and whose status sits next to the buttons;
Maintenance with its own status lines; one Re-enrich menu per library row
instead of three stacked buttons; Reclassify with a selection bar.

Nothing in the backend changed except the `reason=` filter on
`/api/enrichment/unmatched` and the two fixes above; a browser reload is
enough.

**Code scanning.** CodeQL alert #54 (`py/bad-tag-filter`): the frontend
syntax test extracts the inline scripts with `html.parser` instead of a
tag regex, so upper-case tags and end tags with attributes are the
parser's business, not a pattern's. Alert #53 (clear-text storage, the
`.env` writer) is by design — `.env` is the secret store, see SECURITY.md —
and is now suppressed on the line itself instead of re-dismissed each time
the writer moves; reading it again did find a real gap: the temp file was
created with the directory's default rights and narrowed only after the
rename. It is now owner-only from the first byte.
Bandit #52 (B608, the recommendation pitch prompt): the `# nosec` marker sat
inside the f-string and reached the model as prompt text; it is gone, and
the mission line no longer reads like SQL to a pattern matcher.
Dependencies: pydantic 2.13.5; the workflows moved to github-script 9 and
upload-artifact 7 (Dependabot #71–#73, compatibility read before merging).

**The weekly LLM sweep gets a memory.** Its first Monday after the release
produced 733 medium+ findings and filed the ten alphabetically first as
issues (#74–#83) — every one a re-report of a finding closed as by-design
four days earlier, because the model rewords a finding each week and the
step only knew open issues by exact title. Now: the sweep covers the
server code only (owner-run CLI tools, `scripts/`, the test suite and
vendored code are out — their findings were by-design without exception);
findings are ordered worst-first, deduplicated against every security
issue ever filed, and filtered through `.github/llm-scan-accepted.json`,
where the owner's documented decisions live so the next run can read them;
issues are filed only from `ISSUE_MIN_SEVERITY` up (critical by default),
while the whole picture lands as a digest table on the run page plus the
report artifact. The ten re-filed issues are closed with this change.

**Dependencies keep up with the pins.** `start.bat` proved that packages
were present, not that they matched `requirements.txt`, so a weekly
Dependabot bump passed unnoticed; it also imported `Crypto`, which is
neither pinned nor used, so a fresh install ran pip on every start. Both
launchers now compare the pinned versions with their interpreter before
the first import (`src/deps_check.py`, stdlib only) and install what is
missing or outdated. The server never installs; it logs the comparison at
boot and shows it to admins in Settings → Maintenance, with the command.

**The frontend is three files.** `frontend/index.html` (7,376 lines) split
into markup, `css/app.css` and `js/app.js`, the latter loaded as one ES
module — no framework, no bundler, byte-identical content. The 130
functions the inline handlers name are exported to `window` explicitly and
a test pins that list; a strict-mode audit found no implicit globals; the
three files are served with `Cache-Control: no-cache` so an update never
mixes old code with new markup. Behaviour is unchanged; this is the first
of three steps (next: modules per view, then event delegation in place of
inline handlers). Extraction by Jules from a written brief, finished here.

**Modules per view.** The 6,014-line app module became an entry file plus 25
modules cut along the file's own section banners, with `state.js` holding
the 20 variables more than one module shares; no framework, no bundler,
every function moved once and unchanged. Review of the agent's PR found
and fixed three classes of runtime error the tests could not see: six
initial values defaulted to `null` where the code expects `[]` or `{}`, six
functions that inline handlers call in second position (Curation section
toggles, the deletion checkbox) missing from the window export block since
the first split, and handler strings naming a former global variable or
module state. The hygiene test now checks every identifier in handler code.
Also merged: `aria-hidden` on 22 decorative icons, the image proxy
validating the URL with the same parser that fetches it (no parser
differential), and the history sync status computed in one grouped query.

**The curator's duplicate claims are true again.** Technical profiles were
kept per Plex rating key for ever, so every re-add or library move left a
phantom copy behind: 1,373 titles looked duplicated, and the curator told
the owner he kept two copies of a series that exists once. The tech sync
now prunes profiles whose rating key Plex no longer has (only for sections
it fetched completely), the redundancy audit collapses identical files
under different keys, and the curator is told it executes nothing itself:
a declared intention to watch is acted on by the app after the reply, and
the real outcome (protected; watchlist added or refused; downscale flagged
or not) now arrives as a notification instead of a claim.

**Event delegation.** The 217 inline handlers — 109 in the markup, 108 in
the view templates — are data attributes dispatched by one listener set in
the entry module; a registry names what an attribute may call, arguments
travel as JSON, handlers that carried a condition or two statements became
one-line functions, and the helpers that used to build handler strings
(retry buttons, menus, pagers, empty-state actions) take attribute text
instead. Nothing runs in the global scope any more: the window export block
is gone and the Content-Security-Policy's `script-src` is `'self'`, so
injected markup cannot execute code. Both halves converted by Jules from
written briefs; the second arrived without the imports the templates need
and with variables passed as strings — finished here and pinned by tests.

**Three small ones from a screenshot.** The metadata audit failed its run
whenever it met the same finding twice in one pass (the app's sessions do
not autoflush, so the upsert could not see the row it had just added); it
sees it now. A failed custodian step put the whole database error, statement
and parameters included, into the status pill; one line now, the rest in
the log. The Activity view rebuilt every task row on every progress event
and replayed the card animation each time; rows are patched in place and
renders coalesce to one per frame.

**The curator reads lyrics.** SoulSync leaves .lrc/.txt sidecars next to
the tracks and Plex serves them as lyric streams; a collector keeps the
plain lines in its own cache (13,390 of 18,648 tracks on the owner's
server carry one) and re-checks every run so late sidecars land, and a
profiler condenses a sample per artist into a lyrics profile: subjects,
themes, languages, an explicit flag, motifs, tone, up to three verbatim
lines. The music summariser makes lyrical claims only from that profile
(before, it guessed "lyrical themes" from a biography), the verified block
shows it with its basis, the album dossier adds coverage and one line, the
constitution caps quotes at two short lines. Raw lyrics never reach a
prompt or the UI.


**Smaller things from the second September round.** The Knowledge Base
status counts its history in one grouped query instead of two per category.
Existence checks at boot and login select an id instead of counting rows or
building an object. The image proxy fetches the URL it validated rather than
the raw string it was handed. Dependencies: uvicorn 0.53, SQLAlchemy 2.0.54,
PyJWT 2.14, and the CodeQL and OSV actions on their latest pins. The push
showed what a partial Dependabot round does: osv-scanner 2.6.0 rejects the
`--skip-git` flag the workflow still passed, and `codeql-action/init` sat a
version behind `analyze`, which CodeQL refuses to run. Both fixed;
`test_workflow_pins` keeps every action family on one commit and the
Dependabot config groups a family into one PR. Two more Bolt merges: the Spotify statistics on the Libraries page come from one aggregate query instead of five, and create_user checks for an existing user by id. The first OSV run that
really scanned (the old reusable workflow had answered "No issues found"
from a missing results file) listed the four chromadb server advisories
SECURITY.md already explains, now excepted in `osv-scanner.toml` and
guarded by `test_osv_config`, plus floor versions of transitive packages
no install gets, so the scan now resolves the direct pins only.

**GPU work no longer holds up the rest.** The data custodian ran every
maintenance task in one line: a scheduled deletion scan held the GPU for
almost two hours, every tick in between was skipped, and the editions walk,
which needs no model at all, never ran. Tasks that drive a model now run in
one queue and everything else in a second, side by side. The few
dependencies across them are explicit: the deletion scan and the post-sync
taste run wait for the Plex sync so the recently-watched veto sees last
night's plays, and the playlist pushes defer until the recommendation
refresh is through instead of blocking. The Plex sync's model half (taste
vectors with the curator's summary, the recommendations cache) moved into
the model queue, the ARR prefetch, which runs the summariser, is filed as
model work, and the Knowledge Base names what each queue is on.

**One Python per start.** `start.bat` ran whichever `python` came first on
PATH, and PATH differs between consoles: one start ran on PlatformIO's
Python, the next on Python 3.12, each raised only when it happened to run,
and both environments are shared with other tools whose own pins the raises
broke. The launchers now pick a venv in the folder, else `py -3.12`, else
PATH, print the choice, and every start logs it; USAGE explains how to give
Curatarr its own venv. A fresh venv is filled with the tested lock, exact
and hash-checked, instead of whatever requirements.txt resolves to that
day, and an empty environment no longer reports that it matches the lock.

**Smaller.** The user pill in the top bar opens Settings → Account and is
reachable by keyboard (from Jules' #129), and the sidebar marks the open
view however it was opened. The classifier's action-line splitter is linear
again after CodeQL found the first version quadratic on long runs of spaces.
The test battery logs to `data/logs/tests.log`, so the app's own log shows
only the app.

**Movies are not anime because their title says "to".** A title guess
decided for every movie whether it was anime, and "to", "no", "de" and
"san" counted as Japanese particles: 195 movies, *Back to the Future*
among them, went to the anime catalogues instead of TMDB and 120 never got
a profile. With Jikan unreachable each of them failed as an outage, so
five in a row ended the movie lane of every run within a minute while
2,000 movies waited behind them. The library's own filing decides now
(the Anime section, Sonarr's anime type), and only an outage where no
source answered at all counts toward ending a lane.

**Keep working while you are away.** Knowledge Base → Maintenance has a
**Keep working** button with an hour picker. Until then the custodian looks
for due GPU work every two minutes instead of every thirty, with the large
budgets, and asks Windows not to idle-sleep, which it otherwise does after
15 minutes without input. Beside the other GPU work the enrichment
pipeline keeps going in large chunks, so a task that mostly waits on the
network no longer leaves the card idle. It ends at that time, on **Stop**,
or by itself once nothing is due. The enrichment cycle also stays due while
its budget leaves items behind, instead of resting a day after 400 of 2,300.

**The Knowledge Base no longer waits for Lidarr.** Its overview needs the
library lists of Sonarr, Radarr and Lidarr, and once the 15-minute cache
window had run out the next page load fetched them itself. Lidarr's artist
list takes a minute, or two 90-second attempts when it hangs: 185 to 198
seconds per load on 2026-10-06. An expired list is now served at once and
refreshed behind the request; only a forced refresh or a first start
waits. An arr that is not configured no longer serves its old list, so
switching Lidarr off moves music to the Plex index at once, and the
SoulSync catalogue sync skips its nine-minute walk without Lidarr.

**Proposals of a switched-off service are retired.** With Lidarr switched
off, its open deletion proposals could never be carried out: a delete
reported "Lidarr is currently unreachable" and parked the proposal in limbo
for good, and the daily scan only replaces pending ones. A delete now says
the service is switched off and retires the proposal (nothing is deleted),
and the scan retires the rest; music comes back from the Plex index.

**Deleted titles that come back are shown.** An arr list, a request or
SoulSync's playlist sync can put a deleted title back. A daily check looks
every deleted proposal up in its library, by TMDb, TVDb or MusicBrainz id,
or by a unique title added after the delete (same-named films are common),
and the Deletions view lists the returns under "Back after deletion" with
Delete again and Keep.

**A deleted artist is blocked in SoulSync.** SoulSync's playlist sync
compares against Plex, so a liked song of a deleted artist went back on its
wishlist at the next sync. Deleting music now puts the artist on SoulSync's
blocklist, reads the entry back and warns when it did not hold; a ban that
did not hold is retried once a day, and the delete dialog says the artist
will be blocked. The warning is a fixed sentence, an exception's text stays
in the log (CodeQL). This is Curatarr's only write to SoulSync; it blocks
downloads and never starts one.

**The image proxy keys its cache by the parsed URL**, the one it fetches,
so two spellings of one URL share one cache file. Four automated PRs had
called the raw-string key an SSRF parser differential; it was not one, the
host check and the fetch always used the same parsed URL.

**SoulSync follows and likes shape music taste.** An artist followed in
SoulSync counts like a 5-star rating in the music taste vector, even before
the first play, and is never proposed for deletion; a Spotify like still on
SoulSync's wishlist counts like 4 stars. SoulSync's retry batches and
discovery mixes stay out.

**A restart no longer hangs on the custodian.** The enrichment run swallows
a cancel and returns, so when a restart caught the custodian in an
enrichment step, its queue went on with the next tasks after "shutting
down" and the old process never ended; on Windows the reloader waits for it
without a limit. The queue now stops before its next task once it has been
cancelled.

**Fewer "is this a game?" questions, and Steam games recognised again.**
Every running program nobody had classified got the question, so the "not a
game" list had grown to 530 names, 95 of them versions of the same seven
installers and updaters (68 InstallShield extractors alone). One answer now
covers every version of a program, installers, updaters and crash handlers
are never asked about, and neither is anything holding less than 500 MB. Steam's
overlay, the best sign that a game runs, had moved to a 64-bit process the
detection did not know.

**Clean genres from SoulSync.** SoulSync hands out its artist genres as a
JSON list cut at the commas and mixed with clean values (`'["Comedy'`,
`'Comedy Rock"]'`), for 100 of 120 artists in a sample. Since SoulSync's
fields win the artist-profile merge, the pieces reached the profile, its
embedding text and the summariser's input. Curatarr now strips the brackets
and quotes from each piece; similar-artist names keep quotes of their own.

**MyAnimeList instead of Jikan.** Jikan, the unofficial MyAnimeList
scraper, has failed since 2026-08-28. Anime that AniList could not match
waited on it in every run, and anime reception, which wanted MAL's scores
and reviews from it, stood still for every title with a MAL id. With a free
MyAnimeList client ID (Settings → Integrations) Curatarr now asks the
official API for scores, genres, the content rating and the title search.
Without one it still asks Jikan, but no longer than three failures in a
row before an hour's pause; reception then goes on with the AniList reviews.

**Dependencies.** PyJWT 2.15.0 (GHSA-42vr-xj54-vc7v; Curatarr only verifies
signed tokens, so the pre-verification path was never used), oauthlib 4.0.0
for two provider-side advisories (it arrives only through kubernetes, which
the embedded ChromaDB never loads) and multidict 6.9.1.

**No inline styles.** The Content-Security-Policy's `style-src` is
`'self'`: the 278 inline style attributes in the markup and the module
templates became classes (a utilities block named by property, a handful
of named boxes), the eight computed values (progress widths, poster sizes,
bar colours) travel as `data-style` and are applied from script by
`ui.hydrateStyles`, and every element that started hidden uses the
`hidden` attribute. The frontend-hygiene ceilings for inline styles are
zero now; a `data-style` ceiling of eight takes their place.

**The audit's residuals, closed.** A household member's Activity view
now lists only the tasks that carry their user id, the task stream the
same, and the library status tells a member which arr services exist
without their LAN addresses, root folders or test results. OMDb calls are
counted per day and stop at the budget (`OMDB_DAILY_LIMIT`, the free
tier's 1,000) instead of running into refusals until midnight. The
protection classifier's action line splits only before a known field
label, so a title with a pipe in it is protected as one title. The image
proxy resolves a whitelisted name and refuses private, loopback and
link-local answers before every upstream connection.

**What the review left open, closed.** A Plex library answering an error
no longer moves the sync watermark past its plays (the next sync asks for
them again; the result names the library); the library router's
"re-fetch metadata" clears embedding caches under the key format they
actually use (the bare `emb:{id}` it deleted matched nothing since the
model tag joined the key); the Plex PIN nonce travels as a header instead
of in the URL; a parked proposal says "parked" and why, not "delete
failed — unreachable"; the proactive and source-upgrade jobs report a
failure as a failure. `python -m src.deps_lock --compile --upgrade` is the
deliberate lock refresh that moves every version, plain `--compile` keeps
what the lock holds. One model check for every launcher: start.bat asked
`ollama show nomic-embed-text` by name and printed "models missing" on an
install that runs v2-moe from the stored profile; start.bat, the tray and
`build_models.py --check` now share one list, and an Ollama that does not
answer is reported as that, not as every model missing.

**Seven hardening pull requests, reviewed and merged (#122 to #128).** A
review of the auth, deletion, sync, chat and enrichment paths: model
output can no longer fire app actions (DOMPurify kept `data-on-*` and the
dispatcher ran them without a click); the setup gate honours logout and
deactivation, `.env` values with line breaks are refused, wrong setup
codes and PIN polls are budgeted, a Plex PIN is bound to the browser that
asked for it; a deletion re-checks Keep and protection right before the
DELETE, is claimed atomically, verifies a timed-out delete by GET and
parks the unclear case in limbo, Plex-music proposals get a real probe,
and the protection classifier acts only on titles the user meant; the
Plex sync's watermark is the fetch time, failed or busy jobs are no longer
stamped done, the deletion-proposal indexes lost by the AUTOINCREMENT
rebuild are recreated, sqlite work leaves the event loop; the GPU slot
survives a cancelled eviction, a double-submit is refused before any
work, verification answers land, background polls are not impressions;
re-enrichment replaces vectors (Chroma's `add` kept the old one
silently), stored vectors are checked against the item, cache wipes are
scoped, one backfill worker per source. Python 3.12 is the stated floor.
Finished here: a job that merely waits (game, running enrichment) leaves
no Activity card, and the chat stream's error line no longer carries the
exception text.

**47 packages raised in the lock** by Dependabot's first weekly round on
`lock/`, filelock 4, websockets 17 and protobuf 7 among them. One of its
bumps was impossible: Dependabot edits a uv-compiled lock line by line, no
resolution behind it, and moved pydantic-core to 2.49.0 while pydantic
2.13.5 pins 2.46.5, so the first CI install of the merged lock failed
(ResolutionImpossible). The lock was recompiled with uv, which keeps every
other version and corrects that one; the launchers install the set on the
next start. Dependabot's version PRs for pip are off from here: the lock
is refreshed with `python -m src.deps_lock --compile`, security PRs still
arrive.

**Watchlist adds that land.** "Sounds promising, I'll put it on my
watchlist" has ended deletion debates since August, and every one of those
adds died with "plex.tv returned 400": Discover's search refuses a request
without `searchProviders`. The search now sends the parameters and device
headers plex.tv's own clients send, the error keeps plex.tv's message, and
the add was proved end to end against a real account (search hit, add,
listed, removed again). `tests/test_plex_watchlist_request.py`.

**Series editions looked at daily.** The custodian task ran weekly, so a
series added to Sonarr or a corrected classification could wait seven days
behind a finished walk; the task now looks every day while each row keeps
its weekly recheck.

**Hash-pinned, everywhere it installs.** The lock is now a universal,
hash-pinned requirements file (`uv pip compile --universal
--generate-hashes`, wrapped as `python -m src.deps_lock --compile`), and
so are the two scanning tools CI installs. Every pip install on a runner
carries `--require-hashes`, the launchers raise a package to the lock
through the same check, Dependabot targets the lock and rewrites the
hashes itself, and `requirements.txt` repeats the lock's versions for the
direct pins. The three "pip not pinned by hash" rows in the Security
tab go with it.

**The tested install is written down.** `lock/requirements.txt` lists
every package the 17 pins pull in, at the versions the test battery ran
with (92 today). The launchers keep it honest at every start: a package
that fell below its lock line is raised, a newer one raises the line,
nothing is ever lowered, and the file is rewritten only when something
changed. The security scanners read it, so an advisory in a transitive
package — anyio's two of 2026-09-18 were the trigger — is visible against
the version that actually runs. Settings → Maintenance shows the
comparison next to the pins. The Deletions entry in the sidebar now
pulses while a deletion analysis runs in any tab, and offers a reload
when one started elsewhere finishes.

**The bell stops repeating itself and stops calling a routine a binge.**
Proactive messages had no memory for their pattern subjects, so the same
genre, the same late-night title and the same three dropped shows came
back every morning, and the first detector in a fixed list always won.
Every subject is now remembered for weeks and the pick rotates towards
the types that have been quiet. Binges are measured from real sittings
(plays chained by their own timestamps and lengths) instead of a
six-hour window counted from whenever the scheduler ran, bulk
"mark as watched" rows no longer pass as viewing, and both the bell and
the chat starters repeat the measured rhythm — "6 episodes in one
sitting (2 h 13 min)" or "an episode or two an evening" — instead of
guessing.

**The curator has more than one voice, remembers what you answered, and
says good morning.** Every proactive message now draws a register —
provocative, curious, dry, warm or analytic — never the same twice in a
row, instead of the one teasing voice every message used to wear. When
you answered an earlier nudge about the same series, song or genre, the
next one quotes your answer and builds on it rather than asking again.
And a new "Morning line" (Settings → Notifications) sums up last
night's watching between five and eleven in the morning, once a day,
and is gone by noon. The chat's watch status for a music artist now says plays and distinct tracks; Plex and Spotify rows carry a track index in the episode column, and the curator had read that as "15 episodes".

**Music that arrives later finds the plays that were waiting.** Matching
an imported listening history against your own library used to be a
one-shot per play: whatever was not in Plex that night stayed unmatched
forever, so a collection that grows album by album could never improve its
own numbers. Every pass now also asks the other question — which of the
tracks that just arrived has history waiting for it — using the track index
that is refreshed daily anyway. A quiet day costs a single query.

**A music lookup that finds nothing is remembered.** Artists that
MusicBrainz has no entry for were asked again every night, for years if you
let them: seventeen minutes of rate-limited traffic a day on one library,
for names no database will ever carry. A miss is now written down and waits
two weeks before the next attempt, then a month, then a quarter, and a
later success clears it. The Music-pipeline tab says how many names are
parked, so a progress bar that stops short of full no longer looks broken.
The pipeline also stops calling itself finished after burning a whole batch
on names that answered nothing.

**The sidebar can be used from the keyboard.** Every navigation item is
a real button to assistive technology, reachable with Tab and activated
with Enter or Space, using the same handler the poster cards and chips
already use.

**Smaller things from the same round.** The endpoint-privacy warning now
parses a URL with the same library that performs the request, so the check
and the connection can no longer disagree. The Knowledge Base overview
gets its music-pipeline numbers in one query instead of five. The proactive
triggers read the columns they need instead of building five thousand
objects per run. And a cached endpoint has to say whether its answer is the
same for everybody or has to be kept per user — the safe default is no
default.

**The GPU is busy, so the work moves instead of stopping.** A graphics
card held by another program used to park every model call, and the chat
still tried anyway and died in a timeout. Both halves were wrong, and the
measurement says why: the big curator model cannot even start while the
card is occupied, while the small summariser model runs a real enrichment
on six processor threads in about two and a half minutes without touching
the card at all. So enrichment, significance, reception, memory extraction,
taste vectors and lyrics profiles now keep going on the processor and your
library stays current; six threads are as fast as twelve, so the rest of
the machine stays with whatever is holding the card. A conversation needs
the card back, and Curatarr says so in its own words instead of leaving you
with a spinner. That work needs memory as well as cores — one run measured
9.7 GB — so it also checks that enough is free before it starts, and waits
instead of pushing the machine into swap; when it waits, Curatarr says
that too rather than promising progress that is not happening. A detected
game still parks everything. The badge in the top bar says which of the
three it is instead of calling everything a game, the 30 s watcher records the state and logs every change, and the
setup wizard asks for the behaviour while Settings → Integrations →
"Sharing the graphics card" changes it later without a restart.

**A busy GPU counts as a game.** An image-generation job held the whole graphics card while the game detector, which knows game process names only, reported nothing: the enrichment ran into summariser timeouts for an hour and the custodian kept starting model work. A GPU saturated by something other than Ollama now sets the same flag every LLM-heavy path already yields to, after 45 seconds of sustained pressure; a host without nvidia-smi is left alone, and `GPU_PRESSURE_GATE=0` switches it off.

**Lidarr is optional.** Three set-ups now work: Lidarr alone, Lidarr with
SoulSync, and no Lidarr at all. Without it the daily Plex walk is the music
index — artists with their MusicBrainz ids, albums, tracks and footprint —
and everything that used to ask Lidarr reads it: deletion candidates (and
the deletion itself, through Plex's own API with a re-read that never
mistakes a kept file for a deletion), the Music page with a Wanted tab,
the curator's discography and album evidence, the Knowledge Base counts.
Wanted artists become wishes you fulfil in SoulSync; the list shows when
Plex has them.


**Which cut do you own, and is there an uncensored one?** A weekly walk
reads every Sonarr series' episode files (quality source, release names,
custom formats) and the AniDB tags of the offline snapshot, by AniDB's own
definitions: "tv censoring" means the Blu-ray/DVD release is the uncensored
cut, "censored uncensored version" means even the disc keeps some
censoring. The curator's verified block says "Edition: 24 episode files,
all from broadcast or web, none named uncensored; AniDB: the TV airing was
censored, the Blu-ray/DVD release is the uncensored cut", the Curation
upgrade list shows those titles ("TV cut — uncensored disc release
exists") with a Search-releases button that asks your indexers live for
releases named uncensored or from Blu-ray. The prose the enrichment
collects was measured first and dropped as a detector: it mentions
censorship for half a percent of the anime, half of those as plot.

**A UI audit, end to end.** Every hint, table header and empty state used
a grey of 2.1–2.6:1 contrast; it now clears 4.5:1 on every surface, and so
does the danger red. Reduced motion is honoured (near-zero durations, so
nothing that fades in stays invisible), touch screens get 44 px targets.
Copy says one thing one way: no env-var names or column names in empty
states, Arr apps instead of ARR/\*arr, the encryption "PIN" is a passphrase
and no longer implies encryption that is not active yet, a setup hint no
longer points at a view that does not exist.

**Keep and Delete tell the truth.** Keep pauses a title for 90 days, and
its dialog can protect it for good. Delete dialogs no longer say "cannot be
undone" regardless: they read each arr's Recycle Bin setting and say where
the files go, name the import-list exclusion, and bulk delete lists every
title. Card actions run Keep, Discuss, then Delete set apart.

**A failed analysis is no longer a clean library.** A judge call that timed
out or answered nonsense used to vanish like a title the model could not
decide. Failures are counted per run and the Deletions view says so in a
banner that stays; error toasts stay until dismissed.

**Views have addresses.** The sidebar is links grouped Discover · Library ·
Curate · System; `#recs` or `#settings/users` can be bookmarked, Back and
Forward work, focus lands on the new view's heading, and the active entry
is announced as current. Libraries and Users were second copies of
Settings panes and are now only Settings → Plex libraries and Settings →
Users (with the Spotify import); Stop server moved to Settings →
Maintenance.

**Cut, Your call, and Why.** The "87 %" on a deletion card was the
shortlist score divided by 100, not a confidence. The card now says Cut or
Your call, and **Why?** lists the strongest signals in that score ("+66 Far
from your watch history", "−12 Community rating 8.0/10"). Stored per
proposal; `update_db.py` now runs the column migrations too.


## 2026-09-05 — v1.0.1-beta: the security pass the release deserved

A patch release one day after the first tag, and it exists because the
first tag was followed by the first real *boundary* audit — who can call
what, with whose identity, before and after setup — rather than another
review of code. Two parallel audits plus a live probe (a fresh clone booted
with no `.env`, poked unauthenticated from localhost and from a LAN
address) found things no scanner had, and every fix here was re-verified
against that live instance. No deployment other than the reference
household is known, and that one was checked and unaffected; nobody is
logged out by the upgrade.

**Security fixes**

- **Forgeable sessions after an ordinary admin action.** Reconfiguring an
  arr service through the Library panel handed `write_env` the `SecretStr`
  *objects* for `JWT_SECRET`/`PLEX_TOKEN`; f-strings render those as
  `**********`, which would have been persisted as the secret and reloaded
  live — every token signed with a ten-character constant. Fixed at the call
  site and in `write_env`, which now unwraps secrets and refuses to write a
  mask at all.
- **Any plex.tv account could log in, and the first one became admin.** New
  accounts must now be known to the configured Plex server (its own
  `/accounts` list — the same one watch history is attributed with), and the
  first account must be the server owner. Fails closed for new accounts
  only; `PLEX_LOGIN_REQUIRE_MEMBERSHIP=false` is the escape hatch.
- **Deactivating a user and signing out were cosmetic.** `is_active` was
  never checked per request and the rolling refresh renewed tokens from
  their own stale claims. Every request now consults the live account row
  plus a per-user token version (JWT `ver` claim; new column, automatic
  migration): Sign out revokes on every device, deactivation is immediate.
- **Household members could read each other's current chat topic.** The
  anchor cache was keyed by thread id alone while everyone's free chat is
  thread `general`; `/correct-anchor` also let anyone clear it. Keys are now
  user-scoped.
- **First-run setup was open to the whole LAN** until the first admin
  existed (by necessity — nobody can authenticate yet). A one-time setup
  code, printed to the console and log at startup, is now required from any
  non-localhost device; the wizard prompts for it.
- **The OpenAPI spec was public** at `/openapi.json` despite `ENABLE_DOCS`
  being off — the gate only hid the Swagger UI. Gated together now.
- A Content-Security-Policy and Permissions-Policy on every response; the
  `server` banner is gone; the unauthenticated poster cache has a size
  budget; force-resync is admin-only; the shared music pipeline can be
  stopped only by its starter or an admin; PIN comparison is constant-time;
  `/plex/pin` is rate-limited; a wildcard CORS origin disables credentials;
  `python-jose` replaced by PyJWT (drops the python-ecdsa advisory).

**Setup and robustness**

- `write_env` no longer regenerates `JWT_SECRET` on every wizard re-run
  (which logged the whole household out), keeps tuned sync/binge values,
  writes atomically, narrows the Windows ACL of `.env`, and rejects blank
  Plex fields instead of flipping `FIRST_RUN`. Blank URL values in an
  existing `.env` no longer brick startup. The wizard's Build-models step
  no longer crashes on a cp1252 Windows console; `start.bat`'s dependency
  preflight matches the shipped dependencies again.
- Scanner fleet completed: Bandit, OSV-Scanner and OSSF Scorecard join
  CodeQL; all findings triaged to zero (real ones fixed, the rest justified
  in code); the Best Practices badge entry exists. Full commit history
  swept for leaked secrets: clean.

**Upgrade:** pull, restart. The `users.token_version` column is added
automatically. Existing sessions stay valid.

---

## 2026-09-04 — v1.0.0-beta: the first tagged release

The repo went public on 2026-08-31; this is the first cut considered
stable enough to hand a version number. The days in between were spent
making the public-facing posture real:

- **The scanner fleet is armed.** CodeQL, an LLM security scan
  (GPT-5.6, structured outputs — the template version burned 63 minutes
  of GPT-4o and reported nothing), Bandit (plain pip install, no
  third-party wrapper action), OSV-Scanner (SHA-pinned; its PR job
  hard-fails a change that introduces a known-vulnerable dependency),
  and OSSF Scorecard with the badge in the README. All findings triaged
  to zero: two real CodeQL hits fixed (unescaped regex interpolation),
  the marked.js XSS class closed by vendoring DOMPurify around the one
  `marked.parse → innerHTML` sink, the rest documented as
  false-positive/by-design.
- **The full 896-commit history was swept for leaks** — private keys,
  tokens, JWTs, API-key shapes, real IPs, committed .env files: clean.
  Every IP ever committed is the documentation placeholder.
- **Seven bot PRs reviewed and merged** (one stripped of a misleading
  benchmark), including SQL-side aggregation for the knowledge-base
  overview and batch-scoped Spotify dedup that no longer loads the
  whole listening history into a set.
- **Music evidence grew reception mass.** ListenBrainz global
  popularity (free account token) gives the judge catalogue listens and
  ranks a discussed album inside the artist's own catalogue; Lidarr's
  mirrored secondaryTypes surface the Live/Compilation/Remix share of
  an artist's disk footprint. Found en route and fixed: Lidarr deletion
  candidates carry the artist MBID under a key the judge never read —
  every automated music scan had been matching by name only.
- **The "fully harvest every API we already call" sweep.** TMDB/OMDb
  vote counts, MAL scored_by and AniList popularity now ride the Rating
  line (7.4/10 from 51 votes and from 500k are different facts), movies
  finally print their runtime, MusicBrainz disambiguation and Last.fm
  artist playcount reach the block, OMDb's MPAA rating and TMDB's
  production countries are extracted at all.
- **Day-named conversation starters die at local midnight** instead of
  surfacing "It is Wednesday evening" on a Thursday.

---

## 2026-09-02 — The UI grows up, and the curator learns to converse

- **A frontend worth looking at.** A full prettification pass on the
  single-file UI: emoji sweep, unified big poster cards across
  Recommendations / Deletions / Add-New, amber hover language, sidebar
  collapse, global search, an at-a-glance panel, a last-played strip, a
  curator avatar — and posters finally at w500 instead of a 92-pixel
  thumbnail stretched to card size (the 2,526 already-stored URLs were
  migrated in place). Poster bytes are content-addressed and now cached
  as the immutables they are: view switches stop re-downloading every
  image.

- **Clicking a recently-played title starts a conversation, not a
  tribunal.** The strip's posters open a chat anchored on the user's own
  watch-history row — the episode stated as a FACT from the log ("S2E7,
  last night, finished"), knowledge honestly held at series level, watch
  depth, viewing pattern, verified data and the measured dialogue line
  riding along, and none of the deletion machinery. One thread per work:
  tonight's episode and tomorrow's continue the same conversation.

- **Conversation starters with enforced variety, and bubbles that can
  die.** The three hardcoded landing-page prompts become a background-
  generated pool anchored on concrete facts (a stalled series at 22
  plays, an artist at 73 plays in a fortnight), with the anti-sameness
  rules in code rather than in the prompt: one form per starter per
  batch, no repeated openings, generic candidates dropped, daypart
  tagging at generation. The pool decays — TTL, click retirement, an
  impression cap for the ignored — and proactive messages inherit the
  same mortality: an unclicked bubble now expires instead of squatting
  at the head of the queue for days.

- **A used session never expires mid-click.** The JWT carried a hard 24h
  expiry and nothing renewed it, so the owner was logged out exactly one
  day after each login — observed as a 200 and a 401 six seconds apart
  on the same connection. Tokens live 7 days now, silently reissued in a
  response header once they are a day old: a session lives as long as it
  is used and dies after a week idle.

- **Slow endpoints name themselves, and status stops burning the DB.**
  A pure-ASGI middleware logs any request over 300ms to first byte —
  and its very first sessions paid for it twice: it caught the session
  cliff above, and it showed the Maintenance view's four status
  endpoints recomputing full-table aggregates on every 10-second poll,
  6-8 seconds of database work per cycle competing with real jobs.
  Status payloads are now memoized for 10-30 seconds with single-flight;
  numbers that change at walker pace stop being computed at poll pace.

- **The curator stops inventing motives and arguing from the wrong
  profile.** Three honesty fixes from live discussions: two umlauts from
  a keyboard typo no longer flip the conversation to German (and the
  language directive now tells the model what its default is based on,
  so it never again explains a switch with the user's taste in German
  rap); a movie discussion argues from the movie taste profile instead
  of the whole psyche (the keyword fallback handed it ALL categories —
  including the music taste it later used as an alibi); and the measured
  dialogue numbers now reach every discussion turn — Given (2021) sat in
  the DB at 42.7 words/min while the curator told the owner it had no
  subtitle data and deduced the pacing from mood tags, wrongly.

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
