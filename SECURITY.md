# Security

## Threat model

Curatarr is built for a **trusted home LAN**. It binds to `0.0.0.0` so
household members can reach it, authenticates users via Plex OAuth + JWT,
and keeps behavioural data local — watch history, ratings and taste vectors
never leave, though enrichment does send titles and artist names to public
metadata APIs. It is **not hardened for the open internet** —
do not port-forward it or put it on a public host without adding your
own reverse proxy, TLS and access control.

Defaults that matter:

- `/api/docs` (Swagger) is **off** by default (`ENABLE_DOCS=false`) — it
  would map every endpoint for anyone who finds the port.
- Secrets (`JWT_SECRET`, API keys, Plex token) live only in `.env`,
  which is gitignored. Never commit it.
- Deletion actions require an authenticated session; proposals are never
  executed without an explicit user approval in the UI.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** on this
repository (Security → Report a vulnerability) rather than a public
issue. Include reproduction steps; you'll get a response as time
permits — this is a spare-time project.
