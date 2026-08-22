## Performance optimizations - N+1 Queries
When resolving relations inside a loop, use bulk-fetching (pre-fetching IDs and Titles using SQLAlchemy's `in_` and `or_`) to prevent N+1 queries. Build dictionaries mapping the IDs/titles to the entities for O(1) lookups during the main loop.

## TMDB IDs uniqueness trap
TMDB IDs are not globally unique; they collide between movies and TV shows (e.g. ID 90 can be a film and a show). Always group or map TMDB IDs using a `(namespace, tmdb_id)` tuple where namespace is "movie" for films and "tv" for shows and anime. If a record lacks a reliable media type or category, do not guess; rely on title-matching instead of risking a confidently wrong ID match.
