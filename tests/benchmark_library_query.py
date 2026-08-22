import time
import uuid
import random
from datetime import datetime
from src.database.connection import get_db_session, init_db
from src.database.models import User, WatchHistoryEntry

init_db()

with get_db_session() as db:
    # Create dummy user
    user = User(plex_user_id=str(uuid.uuid4()), plex_username="test_bench")
    db.add(user)
    db.commit()

    # Generate some dummy music history
    print("Generating data...")
    artists = [f"Artist {i}" for i in range(200)]
    for artist in artists:
        for _ in range(random.randint(5, 50)): # plays per artist
            track_title = f"Track {random.randint(1, 10)} by {artist}"
            entry = WatchHistoryEntry(
                user_id=user.id,
                plex_user_id=user.plex_user_id,
                plex_item_id=f"spotify:{uuid.uuid4()}",
                title=track_title,
                media_type="music",
                series_title=artist,
                source="spotify",
                viewed_at=datetime.utcnow()
            )
            db.add(entry)
    db.commit()
    print("Data generated.")

    from sqlalchemy import func
    # The original query for artists
    q = (
        db.query(
            WatchHistoryEntry.series_title,
            func.count(WatchHistoryEntry.id).label("plays"),
            func.max(WatchHistoryEntry.artist_mbid).label("mbid"),
        )
        .filter(
            WatchHistoryEntry.user_id      == user.id,
            WatchHistoryEntry.media_type   == "music",
            WatchHistoryEntry.source       == "spotify",
            WatchHistoryEntry.plex_item_id.like("spotify%"),
            WatchHistoryEntry.series_title.isnot(None),
        )
        .group_by(WatchHistoryEntry.series_title)
    )
    rows = q.order_by(func.count(WatchHistoryEntry.id).desc()).limit(50).all()

    # Original code block for top tracks
    t0 = time.time()
    for _ in range(10): # run a few times
        artists_out = []
        for r in rows:
            top_tracks_rows = (
                db.query(
                    WatchHistoryEntry.title,
                    func.count(WatchHistoryEntry.id).label("p"),
                )
                .filter(
                    WatchHistoryEntry.user_id      == user.id,
                    WatchHistoryEntry.series_title == r.series_title,
                    WatchHistoryEntry.title.isnot(None),
                    WatchHistoryEntry.media_type == "music",
                )
                .group_by(WatchHistoryEntry.title)
                .order_by(func.count(WatchHistoryEntry.id).desc())
                .limit(3)
                .all()
            )
            artists_out.append({
                "artist_name": r.series_title,
                "top_tracks":  [{"title": t.title, "plays": t.p} for t in top_tracks_rows],
            })
    t1 = time.time()
    orig_time = t1 - t0
    print(f"Original Time (N+1): {orig_time:.4f}s")

    # Optimized code block
    t2 = time.time()
    for _ in range(10):
        artists_out_opt = []
        # Pre-fetch all series titles in the current page
        series_titles = [r.series_title for r in rows]

        # We want to fetch all tracks for these series titles, group by title,
        # count plays, and then sort and limit to 3 per artist in Python.

        # Get all relevant rows in one query
        all_tracks = (
            db.query(
                WatchHistoryEntry.series_title,
                WatchHistoryEntry.title,
                func.count(WatchHistoryEntry.id).label("p")
            )
            .filter(
                WatchHistoryEntry.user_id == user.id,
                WatchHistoryEntry.series_title.in_(series_titles),
                WatchHistoryEntry.title.isnot(None),
                WatchHistoryEntry.media_type == "music",
            )
            .group_by(WatchHistoryEntry.series_title, WatchHistoryEntry.title)
            .all()
        )

        # Group in Python
        from collections import defaultdict
        tracks_by_artist = defaultdict(list)
        for row in all_tracks:
            tracks_by_artist[row.series_title].append({"title": row.title, "plays": row.p})

        for r in rows:
            artist_tracks = tracks_by_artist.get(r.series_title, [])
            # Sort by plays desc, then limit 3
            artist_tracks.sort(key=lambda x: x["plays"], reverse=True)
            top_3 = artist_tracks[:3]
            artists_out_opt.append({
                "artist_name": r.series_title,
                "top_tracks": top_3,
            })
    t3 = time.time()
    opt_time = t3 - t2
    print(f"Optimized Time: {opt_time:.4f}s")
    print(f"Speedup: {orig_time / opt_time:.2f}x")

    # Clean up
    db.query(WatchHistoryEntry).filter_by(user_id=user.id).delete()
    db.query(User).filter_by(id=user.id).delete()
    db.commit()
