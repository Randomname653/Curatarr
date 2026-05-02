"""
Curation Engine - Heuristic-based deletion ranking and proposal generation.

Implements the multi-factor scoring algorithm for storage-efficient deletion decisions.
"""

import json
import logging
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class MediaMetadata:
    """Unified representation of a media item."""
    media_id: str
    service: str  # "radarr", "sonarr", "lidarr"
    title: str
    size_on_disk_mb: float
    added_date: datetime
    last_watched: Optional[datetime]
    watch_count: int
    user_ratings: List[float]
    genres: List[str]
    is_on_watchlist: bool
    is_protected: bool  # Archive, criterion, etc.
    re_watch_probability: float
    metadata_ids: Dict[str, str]  # tmdb_id, tvdb_id, etc.


@dataclass
class DeletionProposal:
    """Recommended deletion with reasoning."""
    media_id: str
    service: str
    title: str
    reason: str
    storage_freed_mb: float
    confidence: float  # 0.0-1.0
    factors: Dict[str, float]
    deletion_score: float


class CurationHeuristics:
    """Multi-factor scoring algorithm for deletion decisions."""

    def __init__(
        self,
        storage_efficiency_weight: float = 0.35,
        temporal_decay_weight: float = 0.40,
        user_affinity_weight: float = -0.15,
        popularity_discount_weight: float = 0.10,
        temporal_decay_threshold_months: int = 12
    ):
        """Initialize weights for heuristic factors."""
        self.weights = {
            "storage_efficiency": storage_efficiency_weight,
            "temporal_decay": temporal_decay_weight,
            "user_affinity": user_affinity_weight,
            "popularity_discount": popularity_discount_weight
        }
        self.temporal_threshold = temporal_decay_threshold_months

    def score_candidate(
        self,
        media: MediaMetadata,
        normalize_storage: float = 100000.0  # 100GB for normalization
    ) -> Tuple[float, Dict[str, float]]:
        """
        Calculate deletion score with individual factor breakdown.
        
        Returns:
            (score, factors_dict)
        """

        # Immunity checks: ABSOLUTE BLOCK
        if media.is_on_watchlist or media.is_protected:
            return (-999.0, {"immunity_override": -999.0})

        # FACTOR 1: Storage Efficiency
        # Larger files + lower re-watch prob = higher score
        storage_score = (
            (media.size_on_disk_mb / normalize_storage) *
            (1.0 - media.re_watch_probability)
        )
        storage_score = min(1.0, max(0.0, storage_score))

        # FACTOR 2: Temporal Decay
        # Unwatched longer = higher score
        if media.last_watched is None:
            months_unwatched = (datetime.now() - media.added_date).days / 30
        else:
            months_unwatched = (datetime.now() - media.last_watched).days / 30

        temporal_score = min(1.0, max(0.0, months_unwatched / self.temporal_threshold))

        # FACTOR 3: User Affinity (PROTECTIVE)
        # High-rated by user = lower (protective) score
        if media.user_ratings:
            avg_user_rating = statistics.mean(media.user_ratings)
        else:
            avg_user_rating = 5.0  # Default neutral

        affinity_score = 1.0 - (avg_user_rating / 10.0)
        affinity_score = min(1.0, max(0.0, affinity_score))

        # FACTOR 4: Popularity Discount
        # Popular items = more protective (lower score)
        tmdb_score = float(media.metadata_ids.get("tmdb_rating", 5.0)) / 10.0
        popularity_score = 1.0 - tmdb_score  # Higher IMDb = protective

        # Combine weighted factors
        final_score = (
            (storage_score * self.weights["storage_efficiency"]) +
            (temporal_score * self.weights["temporal_decay"]) +
            (affinity_score * self.weights["user_affinity"]) +
            (popularity_score * self.weights["popularity_discount"])
        )

        final_score = max(0.0, min(1.0, final_score))

        factors = {
            "storage_efficiency": storage_score,
            "temporal_decay": temporal_score,
            "user_affinity": affinity_score,
            "popularity_discount": popularity_score
        }

        return final_score, factors

    def rank_candidates(
        self,
        library: List[MediaMetadata],
        target_free_space_mb: float,
        current_free_space_mb: float,
        max_proposals: int = 100
    ) -> List[DeletionProposal]:
        """
        Rank candidates and return proposals until target capacity reached.
        """

        # Calculate target storage to free
        storage_deficit = target_free_space_mb - current_free_space_mb
        if storage_deficit <= 0:
            logger.info("Storage target already met, no deletions needed")
            return []

        logger.info(f"Targeting {storage_deficit/1024:.1f} TB free space")

        # Score all candidates
        scored_candidates = []
        for media in library:
            score, factors = self.score_candidate(media)
            scored_candidates.append((media, score, factors))

        # Sort by score (descending) and filter out negative/immunity items
        scored_candidates = [
            (m, s, f) for m, s, f in scored_candidates if s > 0
        ]
        scored_candidates.sort(key=lambda x: x[1], reverse=True)

        # Accumulate proposals until storage target reached
        proposals = []
        total_freed_mb = 0

        for media, score, factors in scored_candidates:
            if len(proposals) >= max_proposals:
                break

            if total_freed_mb >= storage_deficit:
                break

            reason = self._generate_reason(media, factors, total_freed_mb, storage_deficit)

            proposal = DeletionProposal(
                media_id=media.media_id,
                service=media.service,
                title=media.title,
                reason=reason,
                storage_freed_mb=media.size_on_disk_mb,
                confidence=score,
                factors=factors,
                deletion_score=score
            )

            proposals.append(proposal)
            total_freed_mb += media.size_on_disk_mb

        logger.info(
            f"Generated {len(proposals)} proposals "
            f"freeing {total_freed_mb/1024:.1f} TB"
        )

        return proposals

    def _generate_reason(
        self,
        media: MediaMetadata,
        factors: Dict[str, float],
        accumulated_freed: float,
        target_free: float
    ) -> str:
        """Generate human-readable deletion reason."""

        if media.last_watched is None:
            months = (datetime.now() - media.added_date).days / 30
            return (
                f"Never watched. Downloaded {int(months)} months ago. "
                f"Size: {media.size_on_disk_mb/1024:.1f} GB"
            )

        months_since = (datetime.now() - media.last_watched).days / 30

        if months_since > 12:
            return (
                f"Unwatched for {int(months_since)} months. "
                f"Low re-watch probability ({media.re_watch_probability:.1%}). "
                f"Would free {media.size_on_disk_mb/1024:.1f} GB"
            )

        if media.watch_count == 1 and months_since > 6:
            user_rating = statistics.mean(media.user_ratings) if media.user_ratings else 5.0
            return (
                f"Watched once {int(months_since)} months ago. "
                f"User rating: {user_rating:.1f}/10. "
                f"Storage: {media.size_on_disk_mb/1024:.1f} GB"
            )

        # Generic case
        progress = min(100, int(100 * accumulated_freed / max(target_free, 1)))
        return (
            f"Low engagement ({media.watch_count} watch(es)). "
            f"Re-watch probability: {media.re_watch_probability:.1%}. "
            f"Progress to storage target: {progress}%"
        )

    def apply_immunity_protocol(
        self,
        proposals: List[DeletionProposal],
        watchlist_ids: set,
        protected_tags: set
    ) -> List[DeletionProposal]:
        """
        Filter proposals through immunity protocol.
        
        ABSOLUTE PROTECT:
        - All watchlist items
        - Items tagged with protected_tags
        """

        filtered = [
            p for p in proposals
            if p.media_id not in watchlist_ids and
            not any(tag in protected_tags for tag in p.factors.get("tags", []))
        ]

        removed = len(proposals) - len(filtered)
        if removed > 0:
            logger.info(f"Immunity protocol removed {removed} items from deletion")

        return filtered


class StorageAnalyzer:
    """Analyze storage and library composition."""

    @staticmethod
    def analyze_library(library: List[MediaMetadata]) -> Dict:
        """Generate storage analysis report."""

        total_size = sum(m.size_on_disk_mb for m in library)
        total_unwatched = sum(
            m.size_on_disk_mb for m in library
            if m.last_watched is None
        )

        # Temporal distribution
        now = datetime.now()
        distributions = {
            "unwatched": 0,
            "unwatched_12m": 0,
            "unwatched_6m": 0,
            "watched_active": 0
        }

        for media in library:
            if media.last_watched is None:
                distributions["unwatched"] += media.size_on_disk_mb
            else:
                months_since = (now - media.last_watched).days / 30
                if months_since > 12:
                    distributions["unwatched_12m"] += media.size_on_disk_mb
                elif months_since > 6:
                    distributions["unwatched_6m"] += media.size_on_disk_mb
                else:
                    distributions["watched_active"] += media.size_on_disk_mb

        return {
            "total_items": len(library),
            "total_size_mb": total_size,
            "total_size_tb": total_size / (1024 ** 2),
            "total_unwatched_mb": total_unwatched,
            "unwatched_percentage": (total_unwatched / max(total_size, 1)) * 100,
            "temporal_distribution": {
                k: f"{v/1024:.1f} GB" for k, v in distributions.items()
            },
            "services": {
                service: {
                    "count": len([m for m in library if m.service == service]),
                    "size_gb": sum(m.size_on_disk_mb for m in library if m.service == service) / 1024
                }
                for service in set(m.service for m in library)
            }
        }

    @staticmethod
    def generate_report(analysis: Dict) -> str:
        """Generate human-readable storage report."""
        return f"""
Storage Analysis Report
=======================
Total Items: {analysis['total_items']}
Total Size: {analysis['total_size_tb']:.1f} TB
Unwatched: {analysis['unwatched_percentage']:.1f}% ({analysis['total_unwatched_mb']/1024:.1f} GB)

Temporal Distribution:
- Never watched: {analysis['temporal_distribution']['unwatched']}
- Unwatched >12 months: {analysis['temporal_distribution']['unwatched_12m']}
- Unwatched 6-12 months: {analysis['temporal_distribution']['unwatched_6m']}
- Actively watched: {analysis['temporal_distribution']['watched_active']}

By Service:
{json.dumps(analysis['services'], indent=2)}
"""


if __name__ == "__main__":
    # Demo
    sample_library = [
        MediaMetadata(
            media_id="movie_1",
            service="radarr",
            title="Watched Recently",
            size_on_disk_mb=10000,
            added_date=datetime.now() - timedelta(days=30),
            last_watched=datetime.now() - timedelta(days=3),
            watch_count=5,
            user_ratings=[8.0, 7.5],
            genres=["Action"],
            is_on_watchlist=False,
            is_protected=False,
            re_watch_probability=0.8,
            metadata_ids={"tmdb_id": "123", "tmdb_rating": "7.5"}
        ),
        MediaMetadata(
            media_id="movie_2",
            service="radarr",
            title="Never Watched Movie",
            size_on_disk_mb=50000,
            added_date=datetime.now() - timedelta(days=500),
            last_watched=None,
            watch_count=0,
            user_ratings=[],
            genres=["Drama"],
            is_on_watchlist=False,
            is_protected=False,
            re_watch_probability=0.05,
            metadata_ids={"tmdb_id": "456", "tmdb_rating": "6.0"}
        ),
    ]

    analyzer = StorageAnalyzer()
    analysis = analyzer.analyze_library(sample_library)
    print(analyzer.generate_report(analysis))

    heuristics = CurationHeuristics()
    proposals = heuristics.rank_candidates(
        library=sample_library,
        target_free_space_mb=500000,
        current_free_space_mb=100000,
        max_proposals=10
    )

    print("\nDeletion Proposals:")
    for p in proposals:
        print(f"- {p.title}: {p.reason}")
