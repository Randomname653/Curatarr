"""Read-only RAM snapshot of a running Curatarr container (Linux / glibc).

Run INSIDE the container, from the NAS shell:

    docker exec -i $(docker ps -qf name=curatarr | head -1) python - < nas_rambench.py

or pasted into the TrueNAS app's shell as ``python /dev/stdin``. It touches no
data: part 1 reads /proc of the app process (PID 1, uvicorn), part 2 runs a
throw-away allocation in its own child process to show what glibc keeps after
Python frees a big batch, and what malloc_trim(0) gives back on this libc.
"""
import ctypes
import ctypes.util
import json
import os
import subprocess
import sys


def status(pid="1"):
    out = {}
    try:
        for line in open(f"/proc/{pid}/status"):
            k, _, v = line.partition(":")
            if k in ("VmRSS", "VmHWM", "RssAnon", "RssFile", "Threads"):
                out[k] = v.strip()
    except OSError as e:
        out["error"] = str(e)
    return out


def rollup(pid="1"):
    keep = ("Rss", "Pss", "Anonymous", "Private_Dirty", "Swap")
    out = {}
    try:
        for line in open(f"/proc/{pid}/smaps_rollup"):
            k, _, v = line.partition(":")
            if k in keep:
                out[k] = v.strip()
    except OSError as e:
        out["error"] = str(e)
    return out


def rss_mb():
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


CHILD = r'''
import ctypes, ctypes.util, gc, json, sys
def rss():
    for l in open("/proc/self/status"):
        if l.startswith("VmRSS:"):
            return int(l.split()[1]) / 1024
r = {"start": rss()}
# the shape that hurt: a Chroma page of embeddings decoded as Python floats
# (18.8k x 768) plus per-row dicts, then dropped
rows = [[float(i % 997) * 1e-3 + j for j in range(768)] for i in range(18834)]
meta = [{"title": f"t{i}", "domain": "music", "n": i} for i in range(18834)]
r["peak"] = rss()
del rows, meta
gc.collect()
r["after_free"] = rss()
try:
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
    r["trim_returned"] = libc.malloc_trim(0)
except (OSError, AttributeError) as e:
    r["trim_error"] = str(e)
r["after_trim"] = rss()
print(json.dumps(r))
'''

report = {
    "python": sys.version.split()[0],
    "libc": os.confstr("CS_GNU_LIBC_VERSION") if hasattr(os, "confstr") else None,
    "MALLOC_ARENA_MAX": os.environ.get("MALLOC_ARENA_MAX"),
    "PYTHONMALLOC": os.environ.get("PYTHONMALLOC"),
    "build_sha": os.environ.get("CURATARR_BUILD_SHA"),
    "app_pid1_status": status(),
    "app_pid1_smaps_rollup": rollup(),
}
for label, env in (("default", {}), ("PYTHONMALLOC=malloc", {"PYTHONMALLOC": "malloc"})):
    try:
        out = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True,
                             timeout=300, env={**os.environ, **env})
        report[f"glibc_test[{label}]"] = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001 - report, never fail
        report[f"glibc_test[{label}]"] = {"error": str(e)}
print(json.dumps(report, indent=2))
