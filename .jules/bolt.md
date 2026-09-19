## 2024-05-18 - [Optimize N+1 queries in category loop]
**Learning:** When retrieving per-category counts on a model with a media_type column, looping over categories and issuing 2xN queries creates an unnecessary N+1 problem. The `case` function inside a `func.count(func.distinct(...))` aggregation can resolve different columns into a single grouping, allowing all category statistics to be pulled in a single `group_by` query.
**Action:** Use grouped queries with `case` logic instead of issuing individual queries inside a loop whenever fetching category-level aggregates.
