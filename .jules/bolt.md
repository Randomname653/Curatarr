# Performance Learnings

## String Splitting in Proactive Messages

**Context:** The `detect_genre_absence` function (and others like `detect_new_genre` and `detect_genre_rut`) in `src/services/proactive_messages.py` iterate over a user's watch history entries and perform string splitting and stripping (`genres.split(",")` and `g.strip()`) for each entry.

**Finding:** Although this pattern looks like an inefficient loop, benchmarking confirms it only costs single-digit milliseconds for thousands of history entries. Since these detectors run in a background proactive-message check (and not on a hot path), the optimization offers no measurable real-world performance gain.

**Conclusion:** Implementing local memoization or pre-parsing the genres in `_to_dicts` was rejected. It introduces unnecessary regression risks and parallel fields without any meaningful benefit. Measurement confirms that the current implementation is acceptable for its execution context.
