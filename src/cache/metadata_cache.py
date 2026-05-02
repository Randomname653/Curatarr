"""
ARR Suite LLM - Metadata Cache (Phase A)

SQLite cache for metadata API responses to avoid rate limit issues.
"""

import sqlite3
import json
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path
import logging

from src.config import settings

logger = logging.getLogger(__name__)


class MetadataCache:
    """
    SQLite cache for metadata API responses.
    
    Stores:
    - TMDB movie/series data
    - OMDb data
    - AniDB data
    - Media hashes for deduplication
    
    Features:
    - Cache expiration (7 days default)
    - Hash-based deduplication
    - Automatic cleanup of old entries
    """
    
    def __init__(self, cache_path: Path = None):
        """
        Initialize metadata cache.
        
        Args:
            cache_path: Path to cache database (default: data/cache/metadata.db)
        """
        self.cache_path = Path(cache_path or settings.ENRICHMENT_CACHE)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.conn = sqlite3.connect(self.cache_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
    
    def _init_db(self):
        """Initialize database schema."""
        cursor = self.conn.cursor()
        
        # Media items table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS media_items (
                tmdb_id INTEGER PRIMARY KEY,
                tvdb_id INTEGER,
                anilist_id INTEGER,
                title TEXT,
                year INTEGER,
                genres TEXT,
                overview TEXT,
                rating REAL,
                vote_count INTEGER,
                metadata_hash TEXT,
                enriched BOOLEAN DEFAULT 0,
                enrichment_date TEXT,
                enrichment_source TEXT,
                raw_data TEXT,
                created_at TEXT,
                expires_at TEXT
            )
        """)
        
        # API response cache
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_cache (
                cache_key TEXT PRIMARY KEY,
                response TEXT,
                created_at TEXT,
                expires_at TEXT
            )
        """)
        
        # Media hash index
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_hash ON media_items(metadata_hash)
        """)
        
        # Expiration index
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_expiration ON media_items(expires_at)
        """)
        
        self.conn.commit()
    
    def _generate_hash(self, data: Dict[str, Any]) -> str:
        """Generate hash for media item to detect duplicates."""
        # Create hash from identifying fields
        key_parts = [
            str(data.get('title', '')),
            str(data.get('year', '')),
            str(data.get('tmdb_id', '')),
            str(data.get('tvdb_id', '')),
            str(data.get('anilist_id', ''))
        ]
        return '-'.join(key_parts)
    
    def _expires_at(self, days: int = 7) -> str:
        """Calculate expiration date."""
        return (datetime.now() + timedelta(days=days)).isoformat()
    
    def get_media(self, tmdb_id: int) -> Optional[Dict[str, Any]]:
        """
        Get media from cache.
        
        Args:
            tmdb_id: TMDB ID
            
        Returns:
            Media data dict or None if not cached
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM media_items WHERE tmdb_id = ? AND expires_at > ?",
            (tmdb_id, datetime.now().isoformat())
        )
        
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None
    
    def set_media(self, data: Dict[str, Any], source: str = "unknown"):
        """
        Store media in cache.
        
        Args:
            data: Media data dict
            source: Source of data (tmdb, omdb, anidb, etc.)
        """
        cursor = self.conn.cursor()
        
        # Generate hash for deduplication
        media_hash = self._generate_hash(data)
        
        # Check if already exists
        existing = self.get_media(data.get('tmdb_id'))
        
        if existing:
            # Update existing
            cursor.execute("""
                UPDATE media_items SET
                    title = ?,
                    year = ?,
                    genres = ?,
                    overview = ?,
                    rating = ?,
                    vote_count = ?,
                    metadata_hash = ?,
                    enriched = ?,
                    enrichment_date = ?,
                    enrichment_source = ?,
                    raw_data = ?,
                    expires_at = ?
                WHERE tmdb_id = ?
            """, (
                data.get('title'),
                data.get('year'),
                json.dumps(data.get('genres', [])),
                data.get('overview'),
                data.get('rating'),
                data.get('vote_count'),
                media_hash,
                1,
                datetime.now().isoformat(),
                source,
                json.dumps(data, default=str),
                self._expires_at(7),
                data.get('tmdb_id')
            ))
        else:
            # Insert new
            cursor.execute("""
                INSERT INTO media_items (
                    tmdb_id, tvdb_id, anilist_id, title, year, genres,
                    overview, rating, vote_count, metadata_hash, enriched,
                    enrichment_date, enrichment_source, raw_data, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get('tmdb_id'),
                data.get('tvdb_id'),
                data.get('anilist_id'),
                data.get('title'),
                data.get('year'),
                json.dumps(data.get('genres', [])),
                data.get('overview'),
                data.get('rating'),
                data.get('vote_count'),
                media_hash,
                1,
                datetime.now().isoformat(),
                source,
                json.dumps(data, default=str),
                datetime.now().isoformat(),
                self._expires_at(7)
            ))
        
        self.conn.commit()
    
    def get_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """
        Get cached API response.
        
        Args:
            cache_key: Cache key (e.g., "tmdb:movie:123")
            
        Returns:
            Cached response or None
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM api_cache WHERE cache_key = ? AND expires_at > ?",
            (cache_key, datetime.now().isoformat())
        )
        
        row = cursor.fetchone()
        if row:
            return {
                "response": json.loads(row['response']),
                "created_at": row['created_at'],
                "expires_at": row['expires_at']
            }
        return None
    
    def set_cache(self, cache_key: str, response: Dict[str, Any], days: int = 7):
        """
        Cache API response.
        
        Args:
            cache_key: Cache key
            response: API response dict
            days: Cache expiration in days
        """
        cursor = self.conn.cursor()
        
        # Check if exists
        existing = self.get_cache(cache_key)
        
        if existing:
            cursor.execute("""
                UPDATE api_cache SET
                    response = ?,
                    expires_at = ?
                WHERE cache_key = ?
            """, (
                json.dumps(response, default=str),
                self._expires_at(days),
                cache_key
            ))
        else:
            cursor.execute("""
                INSERT INTO api_cache (cache_key, response, created_at, expires_at)
                VALUES (?, ?, ?, ?)
            """, (
                cache_key,
                json.dumps(response, default=str),
                datetime.now().isoformat(),
                self._expires_at(days)
            ))
        
        self.conn.commit()
    
    def cleanup_expired(self):
        """Remove expired entries from cache."""
        cursor = self.conn.cursor()
        
        cursor.execute("""
            DELETE FROM media_items WHERE expires_at < ?
        """, (datetime.now().isoformat(),))
        
        cursor.execute("""
            DELETE FROM api_cache WHERE expires_at < ?
        """, (datetime.now().isoformat(),))
        
        self.conn.commit()
    
    def get_all_unenriched(self, limit: int = 1000) -> List[Dict[str, Any]]:
        """
        Get all unenriched media items.
        
        Args:
            limit: Maximum number of items to return
            
        Returns:
            List of unenriched media items
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM media_items WHERE enriched = 0
            ORDER BY created_at ASC
            LIMIT ?
        """, (limit,))
        
        return [dict(row) for row in cursor.fetchall()]
    
    def get_all(self, limit: int = 10000) -> List[Dict[str, Any]]:
        """
        Get all media items.
        
        Args:
            limit: Maximum number of items to return
            
        Returns:
            List of all media items
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM media_items
            ORDER BY enrichment_date DESC
            LIMIT ?
        """, (limit,))
        
        return [dict(row) for row in cursor.fetchall()]
    
    def close(self):
        """Close database connection."""
        self.conn.close()


# Example usage
async def main():
    """Demo metadata cache."""
    cache = MetadataCache()
    
    # Test set/get
    test_data = {
        "tmdb_id": 123,
        "title": "Test Movie",
        "year": 2024,
        "genres": ["Action", "Sci-Fi"],
        "overview": "Test movie overview",
        "rating": 8.5,
        "vote_count": 1000
    }
    
    cache.set_media(test_data, source="tmdb")
    retrieved = cache.get_media(123)
    
    print(f"Original: {test_data}")
    print(f"Retrieved: {retrieved}")
    
    assert retrieved['title'] == test_data['title']
    print("✓ Cache working!")
    
    cache.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
