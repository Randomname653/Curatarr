# Security

## Threat model

Curatarr is built for a **trusted home LAN**. It binds to `0.0.0.0` so
household members can reach it, authenticates users via Plex OAuth + JWT,
and keeps behavioural data local — watch history, ratings and taste vectors
never leave, though enrichment does send titles and artist names to public
metadata APIs. It is **not hardened for the open internet** —
do not port-forward it or put it on a public host without adding your
own reverse proxy, TLS and access control.

**Data at rest is not encrypted by the app.** An early design encrypted
taste vectors with a PIN-derived key; it was removed rather than shipped,
because the vector's consumers are background jobs that run when nobody is
present to type a PIN — the server would have to cache the key, which
unmakes the scheme — and because the source data (watch history) sits in
the same database regardless. Use OS disk encryption if the disk itself is
in your threat model.

Defaults that matter:

- `/api/docs` (Swagger) **and** the OpenAPI spec are **off** by default
  (`ENABLE_DOCS=false`) — either would map every endpoint for anyone who
  finds the port. (Until 2026-09 only the UI was gated; the spec stayed
  public at `/openapi.json`.)
- Secrets (`JWT_SECRET`, API keys, Plex token) live only in `.env`,
  which is gitignored. Never commit it. The wizard writes it through a
  temp file that is owner-only from the moment it exists (0600 on POSIX;
  on Windows the inherited ACL is stripped before any content is written)
  and renames it into place atomically. `.env` is the secret store by
  design: the process needs the plaintext at boot with nobody present to
  unlock a key — the same reason data at rest is not encrypted.
- Every response carries a Content-Security-Policy: the UI
  loads nothing from other origins, so `connect-src 'self'` means even a
  script injection that survived DOMPurify cannot phone a token home.
- Deletion actions require an authenticated session; proposals are never
  executed without an explicit user approval in the UI.

## Who can log in

Authentication is Plex OAuth, but a plex.tv account is free to create in a
minute — so holding one is not enough. A new account may log in only if
the configured Plex server itself knows it (owner, Plex Home users, shared
friends — the same `/accounts` list watch history is attributed with), and
the **first** account, which becomes the admin, must be the account that
owns the server token. Both checks fail closed for *new* accounts when
Plex is unreachable; existing users are unaffected. `PLEX_LOGIN_REQUIRE_
MEMBERSHIP=false` disables the gate for the rare setup where `/accounts`
does not list a legitimate member.

## Sessions

JWTs live 7 days and are silently re-issued once a day old — but always
from the live account row, never from the token's own claims. Every
request re-checks `is_active` and a per-user token version: **Sign out**
bumps the version, so the token dies on every device at once, and an
admin deactivating an account ends its sessions immediately instead of at
expiry. (Before 2026-09 both were cosmetic — a deactivated member kept
working, and only rotating `JWT_SECRET` could revoke anything.)

## First-run setup

Until the first admin exists the wizard endpoints have to be open —
nobody can authenticate yet. Because the server binds the whole LAN, a
browser on the machine itself may drive setup freely, while any other
device must present the one-time **setup code** the server prints to its
console (and log) at startup. That closes the window in which a LAN
neighbour could have pointed a fresh install at their own Plex.

## What a member can make the server do

Every household member may chat, search and refresh their recommendations
— that is the product — but each of those turns one request into curator
time on the single GPU or into calls against metadata APIs with quotas.
Since 2026-09 the endpoints that do so carry **per-user budgets** (sliding
windows with a `Retry-After` on refusal) and, where the work is long, a
**one-at-a-time guard**: one chat reply in flight per user, one
recommendation generation per user (the plain `GET` lane included), one
Plex sync server-wide, music pipeline starts and sync triggers a few per
ten minutes. Budgets are generous for a human and tight for a script; the
guards expire on their own, so a dropped stream cannot lock anyone out.
Administrative work (enrichment runs, backfills, migrations, process
classification, deletions, arr writes) is admin-only at the router mount.

## Third-party text in prompts

Overviews, reviews, encyclopedia extracts and bios come from sources
anyone can edit or post to. Before they reach a model they are **fenced**
(`<<<UNTRUSTED_SOURCE:…>>>` … `<<<END_UNTRUSTED_SOURCE>>>`) and scrubbed of
markup, chat special tokens and role markers, and every system prompt that
receives fenced text states that such text is data, never an instruction.
The shared verified-data block — the one renderer behind chat answers,
deletion verdicts and pitches — is fenced as a whole. Model output is
stripped of tag-like markup before it is stored, the chat sink renders
through DOMPurify, and names pushed into Plex are plain printable text.
Studio and director notes distilled from Wikipedia cache for a year, not a
decade, so an upstream correction reaches the evidence.

## Bodies from the outside

Subtitle downloads, proxied poster images and uploaded Spotify archives are
read through bounds enforced *while* streaming (4 MB, 5 MB, 50 MB per zip
member / 400 MB per archive), never buffered first and measured after; the
CPU-bound subtitle metrics run off the event loop so one oversized file
cannot stall every other user's request.

## Known dependency advisories

The pinned `chromadb` release carries four open advisories
(GHSA-f4j7-r4q5-qw2c, GHSA-36p7-vc44-83pf, GHSA-2wm9-hf6c-p5cr,
GHSA-xph7-9rjv-w5fr — pre-auth code injection and tenant-RBAC bypasses).
**All four target the ChromaDB HTTP server** (`/api/v2/...` endpoints and
its server-side authorization providers). Curatarr embeds
`chromadb.PersistentClient` in-process and never starts the Chroma
server, so that API surface does not exist in any Curatarr deployment —
the vulnerable code path is unused. No patched ChromaDB release exists
yet; the pin will move once one does.

## Reporting a vulnerability

Please use GitHub's **[private vulnerability reporting](https://github.com/Randomname653/Curatarr/security/advisories/new)**
on this repository rather than a public issue. Include reproduction
steps; you'll get a response as time permits — this is a spare-time
project.
