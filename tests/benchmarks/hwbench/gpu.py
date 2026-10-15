"""GPU side of the bench: what the card holds, and a ballast to shrink it.

``query``    one nvidia-smi reading (total/used/free MiB, name, driver).
``Sampler``  a background ``nvidia-smi -lms`` stream (VRAM used, GPU load,
             power, temperature) plus psutil system RAM, so a measurement
             gets its peak and mean instead of one before/after pair. The
             system-wide RAM reading is the honest one: Ollama runs the
             model in a child runner process (RAM guard, 2026-09-16).
``Ballast``  simulates a smaller card on the 4090. It allocates device
             memory through the CUDA driver API (nvcuda.dll / libcuda, no
             torch needed), touches it so it is resident, and holds it until
             released. Ollama measures free VRAM when it loads a model, so a
             card with 8 GB free looks like an 8 GB card to its scheduler.
             ``Ballast.to_free`` verifies the result with nvidia-smi and
             raises when the driver did not give what was asked.

Not simulated: memory bandwidth and compute. A model that fits runs at
4090 speed here; matrix.py scales generation speed by bandwidth for other
cards and labels that an estimate.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time

MIB = 1024 * 1024


def query(index: int = 0) -> dict:
    out = subprocess.check_output(
        ["nvidia-smi", f"--id={index}",
         "--query-gpu=name,memory.total,memory.used,memory.free,driver_version",
         "--format=csv,noheader,nounits"], text=True, timeout=15)
    name, total, used, free, driver = [s.strip() for s in out.strip().split(",")]
    return {"name": name, "total_mib": int(total), "used_mib": int(used),
            "free_mib": int(free), "driver": driver}


def compute_apps() -> list[dict]:
    """Processes holding CUDA memory right now (a game, a second Ollama)."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader,nounits"], text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    apps = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3:
            try:
                apps.append({"pid": int(parts[0]), "name": parts[1],
                             "used_mib": int(parts[2]) if parts[2].isdigit() else None})
            except ValueError:
                continue
    return apps


class Sampler:
    """Background VRAM/power/RAM sampling. Use as a context manager, read
    ``summary()`` afterwards. ``mark()`` starts a new window inside it."""

    def __init__(self, index: int = 0, interval_ms: int = 250):
        self.index, self.interval_ms = index, interval_ms
        self.samples: list[tuple] = []   # (t, used_mib, util, power_w, temp_c, ram_avail_mib)
        self._proc = None
        self._thread = None
        self._mark = 0

    def __enter__(self):
        self._proc = subprocess.Popen(
            ["nvidia-smi", f"--id={self.index}",
             "--query-gpu=memory.used,utilization.gpu,power.draw,temperature.gpu",
             "--format=csv,noheader,nounits", f"-lms={self.interval_ms}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        return self

    def _read(self):
        try:
            import psutil
        except ImportError:
            psutil = None
        for line in self._proc.stdout:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 4:
                continue
            try:
                used, util = int(parts[0]), int(parts[1])
                power = float(parts[2]) if parts[2] not in ("[N/A]", "") else None
                temp = int(parts[3])
            except ValueError:
                continue
            ram = psutil.virtual_memory().available // MIB if psutil else None
            self.samples.append((time.monotonic(), used, util, power, temp, ram))

    def __exit__(self, *exc):
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        return False

    def mark(self) -> None:
        self._mark = len(self.samples)

    def summary(self, since_mark: bool = True) -> dict:
        s = self.samples[self._mark:] if since_mark else self.samples
        if not s:
            return {}
        used = [x[1] for x in s]
        util = [x[2] for x in s]
        power = [x[3] for x in s if x[3] is not None]
        ram = [x[5] for x in s if x[5] is not None]
        return {
            "vram_used_peak_mib": max(used),
            "vram_used_mean_mib": round(sum(used) / len(used)),
            "gpu_util_mean_pct": round(sum(util) / len(util)),
            "power_mean_w": round(sum(power) / len(power), 1) if power else None,
            "power_peak_w": max(power) if power else None,
            "temp_peak_c": max(x[4] for x in s),
            "ram_avail_min_mib": min(ram) if ram else None,
            # What the cell took from system RAM: a CPU spill shows up here.
            "ram_taken_mib": max(0, ram[0] - min(ram)) if ram else None,
            "samples": len(s),
        }


class CudaError(RuntimeError):
    pass


def _driver():
    if sys.platform == "win32":
        return ctypes.WinDLL("nvcuda.dll")
    return ctypes.CDLL("libcuda.so.1")


class Ballast:
    """Holds device memory so the card looks smaller. One per process."""

    def __init__(self, index: int = 0):
        self.index = index
        self._cu = None
        self._ctx = ctypes.c_void_p()
        self._blocks: list[ctypes.c_uint64] = []

    def _check(self, rc, what):
        if rc != 0:
            raise CudaError(f"{what} failed (CUresult {rc})")

    def _ensure_ctx(self):
        if self._cu is not None:
            return
        cu = _driver()
        self._check(cu.cuInit(0), "cuInit")
        dev = ctypes.c_int()
        self._check(cu.cuDeviceGet(ctypes.byref(dev), self.index), "cuDeviceGet")
        self._check(cu.cuCtxCreate_v2(ctypes.byref(self._ctx), 0, dev), "cuCtxCreate")
        self._cu = cu

    def hold_mib(self, mib: int, chunk_mib: int = 1024) -> None:
        """Allocate and touch ``mib`` more MiB, in chunks (one huge block can
        fail on a fragmented card where several smaller ones succeed)."""
        self._ensure_ctx()
        left = mib
        while left > 0:
            n = min(chunk_mib, left)
            ptr = ctypes.c_uint64()
            self._check(self._cu.cuMemAlloc_v2(ctypes.byref(ptr), ctypes.c_size_t(n * MIB)),
                        f"cuMemAlloc({n} MiB)")
            # Touch it: an allocation nobody wrote may not be backed yet.
            self._check(self._cu.cuMemsetD8_v2(ptr, ctypes.c_ubyte(0xA5),
                                               ctypes.c_size_t(n * MIB)), "cuMemsetD8")
            self._blocks.append(ptr)
            left -= n
        self._check(self._cu.cuCtxSynchronize(), "cuCtxSynchronize")

    def release(self) -> None:
        if self._cu is None:
            return
        for ptr in self._blocks:
            self._cu.cuMemFree_v2(ptr)
        self._blocks.clear()
        self._cu.cuCtxSynchronize()

    def close(self) -> None:
        self.release()
        if self._cu is not None and self._ctx:
            self._cu.cuCtxDestroy_v2(self._ctx)
        self._cu = None

    def to_free(self, target_free_mib: int, tolerance_mib: int = 192) -> dict:
        """Release, then hold just enough that nvidia-smi reports
        ``target_free_mib`` free (within tolerance). Returns the readings."""
        self.release()
        before = query(self.index)
        need = before["free_mib"] - target_free_mib
        if need < -tolerance_mib:
            raise CudaError(f"only {before['free_mib']} MiB free, cannot simulate "
                            f"{target_free_mib} MiB free")
        if need > 0:
            # The CUDA context itself takes a few hundred MiB on first use;
            # allocate, re-read, and top up or trim once.
            self.hold_mib(need)
            now = query(self.index)
            diff = now["free_mib"] - target_free_mib
            if diff > tolerance_mib:
                self.hold_mib(diff, chunk_mib=256)
            elif diff < -tolerance_mib and self._blocks:
                self.release()
                self.hold_mib(max(0, need + diff), chunk_mib=256)
        after = query(self.index)
        if abs(after["free_mib"] - target_free_mib) > tolerance_mib * 2:
            raise CudaError(f"ballast missed: {after['free_mib']} MiB free, "
                            f"wanted {target_free_mib}")
        return {"free_before_mib": before["free_mib"],
                "free_after_mib": after["free_mib"],
                "held_mib": before["free_mib"] - after["free_mib"]}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
