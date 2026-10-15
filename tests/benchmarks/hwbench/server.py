"""A private ``ollama serve`` for the bench, on its own port.

Flash attention and the KV-cache type are SERVER settings in Ollama
(``OLLAMA_FLASH_ATTENTION``, ``OLLAMA_KV_CACHE_TYPE``), not request
options. Instead of reconfiguring the owner's Ollama, the bench starts its
own server on 127.0.0.1:11435 with exactly the environment a cell needs and
restarts it when the next cell needs another one. It shares the models
directory, so pulls land in the normal store and nothing is downloaded
twice. The owner's server on 11434 keeps running and must stay idle (the
runner checks /api/ps there before every measurement).

``ServerConfig.env()`` is recorded verbatim on every run, so the ledger
says which server settings produced a number.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

BENCH_PORT = 11435


@dataclass(frozen=True)
class ServerConfig:
    flash_attention: str = "auto"     # "auto" (Ollama default) | "1" | "0"
    kv_cache_type: str = "f16"        # f16 | q8_0 | q4_0 (needs flash attention)
    num_parallel: int = 1             # production serialises curator calls
    max_loaded: int = 1

    def env(self) -> dict:
        e = {"OLLAMA_NUM_PARALLEL": str(self.num_parallel),
             "OLLAMA_MAX_LOADED_MODELS": str(self.max_loaded),
             "OLLAMA_KV_CACHE_TYPE": self.kv_cache_type}
        if self.flash_attention != "auto":
            e["OLLAMA_FLASH_ATTENTION"] = self.flash_attention
        return e

    @property
    def label(self) -> str:
        return f"fa-{self.flash_attention}_kv-{self.kv_cache_type}"

    def as_dict(self) -> dict:
        return asdict(self)


def ollama_exe() -> str:
    found = shutil.which("ollama")
    if found:
        return found
    guess = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    if guess.is_file():
        return str(guess)
    raise SystemExit("ollama executable not found on PATH")


class BenchServer:
    def __init__(self, port: int = BENCH_PORT, log_dir: Path | None = None):
        self.port = port
        self.endpoint = f"http://127.0.0.1:{port}"
        self.config: ServerConfig | None = None
        self._proc: subprocess.Popen | None = None
        self.log_dir = log_dir

    def version(self) -> str:
        try:
            return httpx.get(f"{self.endpoint}/api/version", timeout=5).json().get("version", "")
        except (httpx.HTTPError, ValueError):
            return ""

    def ensure(self, config: ServerConfig) -> None:
        if self._proc and self._proc.poll() is None and self.config == config:
            return
        self.stop()
        env = {**os.environ, **config.env(),
               "OLLAMA_HOST": f"127.0.0.1:{self.port}",
               # Keep a model loaded between the calls of one cell; the
               # runner unloads explicitly between cells.
               "OLLAMA_KEEP_ALIVE": "10m"}
        if config.flash_attention == "auto":
            env.pop("OLLAMA_FLASH_ATTENTION", None)   # no inherited override
        log = None
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log = open(self.log_dir / f"ollama-serve_{config.label}.log", "ab")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._proc = subprocess.Popen([ollama_exe(), "serve"], env=env,
                                      stdout=log or subprocess.DEVNULL,
                                      stderr=subprocess.STDOUT if log else subprocess.DEVNULL,
                                      creationflags=flags)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.version():
                self.config = config
                return
            if self._proc.poll() is not None:
                raise SystemExit(f"bench ollama serve exited ({self._proc.returncode}) "
                                 f"— port {self.port} taken?")
            time.sleep(0.5)
        raise SystemExit("bench ollama serve did not come up within 60 s")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=10)
        self._proc = None
        self.config = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


def loaded_models(endpoint: str) -> list[dict]:
    try:
        return httpx.get(f"{endpoint}/api/ps", timeout=10).json().get("models") or []
    except (httpx.HTTPError, ValueError):
        return []


def unload_all(endpoint: str, wait_s: float = 30.0) -> None:
    """Evict every resident model and wait until /api/ps is empty — a model
    still unloading would count against the next cell's free VRAM."""
    for m in loaded_models(endpoint):
        name = m.get("name") or m.get("model")
        try:
            httpx.post(f"{endpoint}/api/generate",
                       json={"model": name, "keep_alive": 0}, timeout=30)
        except httpx.HTTPError:
            pass
    deadline = time.monotonic() + wait_s
    while loaded_models(endpoint) and time.monotonic() < deadline:
        time.sleep(0.5)
