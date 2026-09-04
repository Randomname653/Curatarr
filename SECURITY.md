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

- `/api/docs` (Swagger) is **off** by default (`ENABLE_DOCS=false`) — it
  would map every endpoint for anyone who finds the port.
- Secrets (`JWT_SECRET`, API keys, Plex token) live only in `.env`,
  which is gitignored. Never commit it.
- Deletion actions require an authenticated session; proposals are never
  executed without an explicit user approval in the UI.

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

`python-ecdsa` (transitive, via `python-jose`) carries PYSEC-2026-1325 —
a Minerva timing attack on ECDSA P-256 signing. Curatarr's JWTs are
**HS256** (symmetric `JWT_SECRET`); no code path signs or verifies with
ECDSA keys, so the vulnerable primitive is never exercised. The advisory
disappears entirely if `python-jose` is ever swapped for `PyJWT`.

## Reporting a vulnerability

Please use GitHub's **[private vulnerability reporting](https://github.com/Randomname653/Curatarr/security/advisories/new)**
on this repository rather than a public issue. Include reproduction
steps; you'll get a response as time permits — this is a spare-time
project.
