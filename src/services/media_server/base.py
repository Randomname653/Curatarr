"""The media-server seam: one protocol, plain DTOs, key helpers.

Curatarr talks to exactly one media server per running install, Plex or
Jellyfin (docs/JELLYFIN_DESIGN.md, D1). Everything outside this package
speaks in the types below and never builds a server URL; each server gets
an adapter that implements ``MediaServerAdapter``.

The DTOs carry every identity a ``rebind-server`` run needs to find the
same title on the other server (design §8): the server key, its parents,
season/episode/track numbers, all external ids, and the file path + size.

Keys are strings everywhere. A Plex ratingKey is decimal ("48291"); a
Jellyfin item id is a GUID, stored in one spelling only: 32 lower-case hex,
no dashes. ``key_namespace`` tells a server key from the other values the
``plex_*`` key columns hold (an *arr id "radarr:12", a Spotify URI, a
Netflix placeholder, an ``ext-script:`` key) — it replaces the ``isdigit()``
guesses a GUID would fail (design Appendix A.3).

Nothing here does I/O.
"""
from __future__ import annotations

import posixpath
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Literal, Optional, Protocol, Sequence, runtime_checkable

ServerKind = Literal["plex", "jellyfin"]
LibraryKind = Literal["movie", "show", "music"]
ItemKind = Literal["movie", "series", "season", "episode", "artist", "album", "track"]
PlaySource = Literal["plex", "jellyfin_session", "jellyfin_hook", "jellyfin_poll",
                     "jellyfin_report", "marked"]
KeyNamespace = Literal["server", "arr", "spotify", "netflix", "ext-script", "unknown"]

# ── keys ─────────────────────────────────────────────────────────────────────

# Same bound the deletion path has always applied to Plex keys
# (routers/recommendations._MEDIA_ID): no leading zero, at most ten digits.
_PLEX_KEY = re.compile(r"[1-9][0-9]{0,9}")
_JELLYFIN_KEY = re.compile(r"[0-9a-f]{32}")
_JELLYFIN_ANY = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
                           r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
# The *arr forms the enrichment and ledger code write: "radarr:12",
# "sonarr:7", "lidarr:3", and the ledger's per-episode "sonarr:ep:991".
_ARR_KEY = re.compile(r"(?:radarr|sonarr|lidarr):(?:ep:)?[0-9]+")


def is_plex_key(value) -> bool:
    return isinstance(value, str) and _PLEX_KEY.fullmatch(value) is not None


def is_jellyfin_key(value) -> bool:
    """True only for the stored spelling: 32 lower-case hex, no dashes."""
    return isinstance(value, str) and _JELLYFIN_KEY.fullmatch(value) is not None


def normalize_jellyfin_id(value) -> Optional[str]:
    """A Jellyfin GUID in its one stored spelling, or None when ``value`` is
    not a GUID. Dashed and upper-case forms (some endpoints and plugins send
    them) become 32 lower-case hex, so joins never meet two spellings."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not _JELLYFIN_ANY.fullmatch(s):
        return None
    return s.replace("-", "").lower()


def require_server_key(kind: ServerKind, value) -> Optional[str]:
    """``value`` as a canonical key of server ``kind``, or None. The deletion
    path's validator for server keys (design §9): nothing else may reach a
    DELETE URL. Jellyfin keys must already be in the stored spelling."""
    s = str(value if value is not None else "").strip()
    if kind == "plex":
        return s if is_plex_key(s) else None
    if kind == "jellyfin":
        return s if is_jellyfin_key(s) else None
    return None


def key_namespace(value) -> KeyNamespace:
    """What a value in a ``plex_*`` key column is. Only ``"server"`` values
    belong to the media server: a rebind re-keys those and nothing else."""
    if not isinstance(value, str) or not value:
        return "unknown"
    if is_plex_key(value) or is_jellyfin_key(value):
        return "server"
    if _ARR_KEY.fullmatch(value):
        return "arr"
    if value.startswith("spotify:"):
        return "spotify"
    if value.startswith("netflix:"):
        return "netflix"
    if value.startswith("ext-script:"):
        return "ext-script"
    return "unknown"


# ── DTOs ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExternalIds:
    """Every id a title is known by outside the media server. Plex reads
    them from Guid tags, Jellyfin from ProviderIds."""
    tmdb_id: Optional[int] = None
    tvdb_id: Optional[int] = None
    imdb_id: Optional[str] = None            # "tt0133093"
    tvdb_episode_id: Optional[int] = None
    anilist_id: Optional[int] = None
    anidb_id: Optional[int] = None
    mal_id: Optional[int] = None
    mb_artist_id: Optional[str] = None
    mb_release_group_id: Optional[str] = None
    mb_release_id: Optional[str] = None
    mb_recording_id: Optional[str] = None

    def any(self) -> bool:
        return any(v is not None for v in self.__dict__.values())


@dataclass(frozen=True)
class FileRef:
    """One media file as the server sees it. Plex and Jellyfin usually see
    the same files under different mount points, so matching compares the
    path relative to the library root, never the absolute path."""
    path: str
    size_bytes: Optional[int] = None

    def relative_to(self, roots: Sequence[str]) -> str:
        """The path below the longest library root it lies under, in
        forward slashes; the bare path when no root matches."""
        p = _slashes(self.path)
        best = ""
        for r in roots or ():
            r = _slashes(r).rstrip("/")
            if r and (p == r or p.startswith(r + "/")) and len(r) > len(best):
                best = r
        return p[len(best):].lstrip("/") if best else p.lstrip("/")

    def stem_key(self, roots: Sequence[str]) -> str:
        """The relative path, case- and Unicode-normalized, plus the size:
        the file half of the matching cascade (design §8.3, step 2). Both
        parts must agree for a match."""
        rel = unicodedata.normalize("NFC", self.relative_to(roots)).casefold()
        return f"{rel}|{self.size_bytes if self.size_bytes is not None else '?'}"


def _slashes(path: str) -> str:
    return posixpath.normpath((path or "").replace("\\", "/"))


@dataclass(frozen=True)
class Library:
    key: str
    title: str
    kind: LibraryKind
    roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaItem:
    key: str
    kind: ItemKind
    title: str
    library_key: Optional[str] = None
    year: Optional[int] = None
    parent_key: Optional[str] = None         # season of an episode, album of a track
    grandparent_key: Optional[str] = None    # series of an episode, artist of a track
    parent_title: Optional[str] = None
    grandparent_title: Optional[str] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    # Jellyfin files a double episode as ONE item (IndexNumber 2,
    # IndexNumberEnd 3); Plex lists two. None for a single episode.
    episode_number_end: Optional[int] = None
    disc_number: Optional[int] = None
    track_number: Optional[int] = None
    external_ids: ExternalIds = field(default_factory=ExternalIds)
    files: tuple[FileRef, ...] = ()
    genres: tuple[str, ...] = ()
    added_at: Optional[datetime] = None
    duration_ms: Optional[int] = None


@dataclass(frozen=True)
class ServerUser:
    key: str
    name: str
    is_admin: bool = False
    email: Optional[str] = None              # Plex only; Jellyfin users have none


@dataclass(frozen=True)
class UserItemState:
    """One user's current state of one item, as the server keeps it."""
    user_key: str
    item_key: str
    played: bool = False
    play_count: int = 0
    last_played_at: Optional[datetime] = None
    position_ms: Optional[int] = None
    liked: Optional[bool] = None             # Jellyfin Likes; None = not set
    favorite: bool = False
    rating: Optional[float] = None           # Plex 0-10 scale; Jellyfin: None


@dataclass(frozen=True)
class LiveSession:
    """Something playing right now (Jellyfin /Sessions, history tier A)."""
    session_key: str
    user_key: str
    item_key: str
    position_ms: int
    duration_ms: Optional[int] = None
    paused: bool = False
    device: Optional[str] = None
    client: Optional[str] = None


@dataclass(frozen=True)
class Play:
    """One play, from whichever witness saw it (design §4)."""
    user_key: str
    item_key: str
    played_at: datetime
    source: PlaySource
    position_ms: Optional[int] = None
    duration_ms: Optional[int] = None
    completed: bool = False
    device: Optional[str] = None
    client: Optional[str] = None
    date_only: bool = False


@dataclass(frozen=True)
class Capabilities:
    """What the server can do, so callers branch on facts, not on ``kind``."""
    per_play_history: bool                   # Plex: yes. Jellyfin core: no
    # ``live_sessions()`` answers with what plays right now. Jellyfin: yes
    # (/Sessions, history tier A). Plex: no for now - its history covers
    # plays; /status/sessions can switch it on for a "now playing" badge.
    now_playing: bool
    star_ratings: bool                       # Plex: PlexAmp stars. Jellyfin: likes only
    native_watchlist: bool                   # Plex Discover; Jellyfin uses a playlist
    music_moods: bool
    per_user_tokens: bool                    # Plex writes need each user's token


@dataclass(frozen=True)
class ServerInfo:
    kind: ServerKind
    name: str
    version: str
    server_id: str                           # Plex machineIdentifier / Jellyfin Id


# ── canonical keys (server_id_map) ───────────────────────────────────────────

def _norm_name(s: Optional[str]) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s)).strip()


def canonical_key(item: MediaItem, *, parent_canonical: Optional[str] = None,
                  roots: Sequence[str] = ()) -> Optional[str]:
    """The server-independent identity of ``item`` (design §8.2), strongest
    id first; None when it has nothing a rebind may match on. Seasons,
    episodes without their own id, albums and tracks need their parent's
    canonical key. Titles alone never make a key (§8.3, step 3)."""
    x = item.external_ids
    first_file = item.files[0] if item.files else None
    file_key = f"file:{first_file.stem_key(roots)}" if first_file else None

    if item.kind == "movie":
        if x.tmdb_id:
            return f"tmdb:{x.tmdb_id}"
        if x.imdb_id:
            return f"imdb:{x.imdb_id}"
        return file_key
    if item.kind == "series":
        if x.tvdb_id:
            return f"tvdb:{x.tvdb_id}"
        if x.tmdb_id:
            return f"tmdb-tv:{x.tmdb_id}"
        if x.imdb_id:
            return f"imdb:{x.imdb_id}"
        return file_key
    if item.kind == "season":
        if parent_canonical and item.season_number is not None:
            return f"{parent_canonical}/s{item.season_number:02d}"
        return None
    if item.kind == "episode":
        if x.tvdb_episode_id:
            return f"tvdb-ep:{x.tvdb_episode_id}"
        numbered = item.season_number is not None and item.episode_number is not None
        if parent_canonical and numbered:
            return f"{parent_canonical}/s{item.season_number:02d}e{item.episode_number:02d}"
        # A double episode (S01E01E02.mkv) is two items on one file: the
        # number keeps their keys apart under UNIQUE(entity_type, canonical_key).
        if file_key and numbered:
            return f"{file_key}#s{item.season_number:02d}e{item.episode_number:02d}"
        return file_key
    if item.kind == "artist":
        if x.mb_artist_id:
            return f"mb-artist:{x.mb_artist_id}"
        name = _norm_name(item.title)
        return f"name:{name}" if name else None
    if item.kind == "album":
        if x.mb_release_group_id:
            return f"mb-rg:{x.mb_release_group_id}"
        name = _norm_name(item.title)
        return f"{parent_canonical}/{name}" if parent_canonical and name else None
    if item.kind == "track":
        if x.mb_recording_id:
            return f"mb-rec:{x.mb_recording_id}"
        if parent_canonical and item.track_number is not None:
            return f"{parent_canonical}/d{item.disc_number or 1}t{item.track_number:02d}"
        # A single-file album (one FLAC + cue) puts every track on one file.
        if file_key and item.track_number is not None:
            return f"{file_key}#d{item.disc_number or 1}t{item.track_number:02d}"
        return file_key
    return None


# ── the protocol ─────────────────────────────────────────────────────────────

@runtime_checkable
class MediaServerAdapter(Protocol):
    """One media server. Every method raises on a transport or auth failure
    instead of answering empty: "the server did not answer" must never read
    as "the library is empty" (the orphan sweep and the tech-profile prune
    delete on emptiness)."""

    kind: ServerKind

    def capabilities(self) -> Capabilities: ...

    def is_server_key(self, value) -> bool:
        """This server's key shape: the deletion path's validator (§9)."""
        ...

    def deep_link(self, key: str) -> Optional[str]: ...

    async def ping(self) -> ServerInfo: ...

    # library
    async def libraries(self) -> list[Library]: ...
    def items(self, library: Library, *, kinds: Sequence[ItemKind] = (),
              since: Optional[datetime] = None) -> AsyncIterator[MediaItem]: ...
    async def item(self, key: str) -> Optional[MediaItem]: ...
    async def children(self, key: str) -> list[MediaItem]: ...
    async def key_exists(self, key: str) -> bool: ...

    # people and what they played
    async def users(self) -> list[ServerUser]: ...
    async def user_states(self, user: ServerUser, *,
                          since: Optional[datetime] = None) -> list[UserItemState]: ...
    async def live_sessions(self) -> list[LiveSession]: ...
    async def plays(self, user: ServerUser, *,
                    since: Optional[datetime] = None) -> list[Play]: ...

    # writes
    async def upsert_playlist(self, user: ServerUser, title: str,
                              keys: Sequence[str]) -> str: ...
    async def upsert_collection(self, library: Library, title: str,
                                keys: Sequence[str]) -> str: ...
    async def mark_played(self, user: ServerUser, key: str) -> None: ...
    async def delete_item(self, key: str) -> bool: ...
