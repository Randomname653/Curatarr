"""
Curatarr — Spotify History Importer (CLI wrapper)

The importer lives in ``src/services/spotify_import.py`` and is normally
driven from the GUI: drop the extended-streaming-history files (or the whole
zip) into Setup → Import or Admin → Spotify history import, pick the user,
done. This wrapper keeps the terminal path for headless boxes:

    python import_spotify.py <directory> [--user 1]

``<directory>`` holds Streaming_History_Audio_*.json / endsong_*.json files
straight from Spotify's privacy export.
"""
import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", help="folder with Streaming_History_Audio_*.json")
    ap.add_argument("--user", type=int, default=1,
                    help="Curatarr user id to attach the plays to (default 1)")
    args = ap.parse_args()

    from src.services.spotify_import import pending_files, run_import
    directory = Path(args.directory)
    if not pending_files(directory):
        print(f"no usable Spotify extended-history files in {directory}")
        print("(the basic StreamingHistory*.json export is not importable — "
              "request the EXTENDED streaming history)")
        return 1
    stats = run_import(args.user, import_dir=directory)
    if stats.get("error"):
        print("error:", stats["error"])
        return 1
    print(f"imported {stats['imported']} plays "
          f"({stats['skipped']} skipped, {stats['duplicates']} duplicates) "
          f"from {stats['files']} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
