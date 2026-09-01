## 2024-05-24 - [Avoid micro-optimizing psutil iteration]
**Learning:** Using `attrs` parameter as a kwarg vs positional and removing inner loop `try/except` around `psutil.process_iter` in `src/services/process_monitor.py` does not provide any measurable performance benefit. The overhead of iterating OS processes far outweighs small Python-level micro-optimizations.
**Action:** Do not optimize `psutil.process_iter` parameter passing or exception blocks, and instead focus on larger architectural optimizations (e.g. caching, avoiding OS-level calls altogether) per Bolt's guidelines.
