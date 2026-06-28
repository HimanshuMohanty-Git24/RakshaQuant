"""
Agent Memory Database Module

PostgreSQL-based storage for trading lessons with severity-based
ranking and time-decay relevance scoring.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import Column, DateTime, Float, Index, Integer, String, Text, create_engine, desc
from sqlalchemy.orm import declarative_base, sessionmaker

from src.config import get_settings

logger = logging.getLogger(__name__)

Base = declarative_base()


def decayed_score(base_score: float, age_days: int, decay_days: int) -> float:
    """
    Single source of truth for lesson relevance decay.

    Lessons keep full relevance for ``decay_days``, then decay ~5% per week of age beyond
    that. No artificial floor — a stale lesson is allowed to fall below the cleanup threshold
    so it can be pruned. Previously database.py and scheduler.py used two *different* formulas
    (5%/week vs 10%/day) that overwrote each other; both now call this.
    """
    if age_days <= decay_days:
        return base_score
    decay_factor = float(0.95 ** ((age_days - decay_days) / 7))
    return round(base_score * decay_factor, 6)


class LessonRecord(Base):
    """SQLAlchemy model for stored lessons."""

    __tablename__ = "agent_memory"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    lesson_id = Column(String(50), unique=True, index=True)

    # Classification
    category = Column(String(50), index=True)
    severity = Column(String(20), index=True)  # low, medium, high, critical

    # Content
    description = Column(Text)
    lesson = Column(Text)

    # Context (for relevance matching)
    strategy = Column(String(50), index=True, nullable=True)
    regime = Column(String(20), index=True, nullable=True)
    symbol = Column(String(20), nullable=True)
    context_factors = Column(Text)  # JSON array

    # Trade reference
    trade_id = Column(String(50), nullable=True)

    # Scoring
    base_score = Column(Float, default=1.0)  # Initial relevance
    current_score = Column(Float, default=1.0)  # Decayed relevance
    use_count = Column(Integer, default=0)  # Times injected
    success_count = Column(Integer, default=0)  # Times proved useful

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(UTC), index=True)
    last_used_at = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
    expires_at = Column(DateTime, nullable=True)  # Optional expiry

    # Index for efficient querying
    __table_args__ = (
        Index("idx_category_severity", "category", "severity"),
        Index("idx_strategy_regime", "strategy", "regime"),
    )


@dataclass
class AgentMemoryDB:
    """
    Database for agent learning lessons.

    Provides:
    - Lesson storage with context
    - Time-decay relevance scoring
    - Context-based retrieval
    - Usage tracking
    """

    database_url: str = ""
    decay_days: int = 30
    min_score: float = 0.1  # Lessons below this are candidates for deletion

    _engine: Any = field(default=None, repr=False)
    _session: Any = field(default=None, repr=False)

    def __post_init__(self):
        if not self.database_url:
            settings = get_settings()
            self.database_url = settings.database_url
            self.decay_days = settings.memory_decay_days

        self._initialize_db()

    def _initialize_db(self) -> None:
        """Initialize database connection."""
        try:
            self._engine = create_engine(self.database_url)
            Base.metadata.create_all(self._engine)
            Session = sessionmaker(bind=self._engine)
            self._session = Session()
            logger.info("Agent memory database initialized")
        except Exception as e:
            logger.error(f"Failed to initialize memory database: {e}")
            # Fallback to in-memory SQLite
            self._engine = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(self._engine)
            Session = sessionmaker(bind=self._engine)
            self._session = Session()

    def store_lesson(
        self,
        lesson_id: str,
        category: str,
        severity: str,
        description: str,
        lesson: str,
        trade_id: str | None = None,
        strategy: str | None = None,
        regime: str | None = None,
        symbol: str | None = None,
        context_factors: list[str] | None = None,
        expires_in_days: int | None = None,
    ) -> bool:
        """
        Store a new learning lesson.

        Args:
            lesson_id: Unique lesson identifier
            category: Mistake category
            severity: Severity level (low, medium, high, critical)
            description: What went wrong
            lesson: Actionable lesson
            trade_id: Optional related trade
            strategy: Optional strategy context
            regime: Optional regime context
            symbol: Optional symbol context
            context_factors: Optional additional context
            expires_in_days: Optional expiry (None = no expiry)

        Returns:
            True if stored successfully
        """
        try:
            # Calculate base score based on severity
            severity_scores = {
                "low": 0.5,
                "medium": 1.0,
                "high": 1.5,
                "critical": 2.0,
            }
            base_score = severity_scores.get(severity, 1.0)

            # Calculate expiry
            expires_at = None
            if expires_in_days:
                expires_at = datetime.now(UTC) + timedelta(days=expires_in_days)

            record = LessonRecord(
                lesson_id=lesson_id,
                category=category,
                severity=severity,
                description=description,
                lesson=lesson,
                trade_id=trade_id,
                strategy=strategy,
                regime=regime,
                symbol=symbol,
                context_factors=",".join(context_factors) if context_factors else "",
                base_score=base_score,
                current_score=base_score,
                expires_at=expires_at,
            )

            self._session.add(record)
            self._session.commit()
            logger.info(f"Stored lesson: {lesson_id} [{severity}]")
            return True

        except Exception as e:
            logger.error(f"Failed to store lesson: {e}")
            self._session.rollback()
            return False

    def get_lessons(
        self,
        category: str | None = None,
        strategy: str | None = None,
        regime: str | None = None,
        min_severity: str | None = None,
        limit: int = 10,
        include_expired: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Retrieve relevant lessons with current relevance scores.

        Args:
            category: Filter by category
            strategy: Filter by strategy
            regime: Filter by regime
            min_severity: Minimum severity to include
            limit: Maximum lessons to return
            include_expired: Whether to include expired lessons

        Returns:
            List of lessons sorted by relevance
        """
        try:
            # Apply decay before querying
            self._apply_decay()

            query = self._session.query(LessonRecord)

            # Apply filters
            if not include_expired:
                query = query.filter(
                    (LessonRecord.expires_at.is_(None))
                    | (LessonRecord.expires_at > datetime.now(UTC))
                )

            if category:
                query = query.filter(LessonRecord.category == category)

            if strategy:
                query = query.filter(
                    (LessonRecord.strategy == strategy) | (LessonRecord.strategy.is_(None))
                )

            if regime:
                query = query.filter(
                    (LessonRecord.regime == regime) | (LessonRecord.regime.is_(None))
                )

            if min_severity:
                severity_order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
                min_level = severity_order.get(min_severity, 1)
                valid_severities = [s for s, l in severity_order.items() if l >= min_level]
                query = query.filter(LessonRecord.severity.in_(valid_severities))

            # Sort by current score (relevance)
            query = query.order_by(desc(LessonRecord.current_score))

            records = query.limit(limit).all()
            return [self._record_to_dict(r) for r in records]

        except Exception as e:
            logger.error(f"Failed to get lessons: {e}")
            return []

    def get_top_lessons_for_context(
        self,
        regime: str,
        strategies: list[str],
        n: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Get top N most relevant lessons for current trading context.

        This is the primary method used by agents to get injected lessons.

        Args:
            regime: Current market regime
            strategies: Currently active strategies
            n: Number of lessons to return

        Returns:
            Top N most relevant lessons
        """
        settings = get_settings()
        n = min(n, settings.memory_top_n_lessons)

        # Get lessons matching context
        lessons = []

        # First, get regime-specific lessons
        regime_lessons = self.get_lessons(regime=regime, limit=n)
        lessons.extend(regime_lessons)

        # Then, get strategy-specific lessons
        for strategy in strategies:
            strategy_lessons = self.get_lessons(strategy=strategy, limit=n // 2)
            lessons.extend(strategy_lessons)

        # Also include high-severity lessons regardless of context
        critical_lessons = self.get_lessons(min_severity="high", limit=n // 2)
        lessons.extend(critical_lessons)

        # Deduplicate and sort by score
        seen_ids = set()
        unique_lessons = []
        for lesson in lessons:
            if lesson["lesson_id"] not in seen_ids:
                seen_ids.add(lesson["lesson_id"])
                unique_lessons.append(lesson)

        unique_lessons.sort(key=lambda x: x.get("current_score", 0), reverse=True)
        return unique_lessons[:n]

    def mark_used(self, lesson_id: str, was_successful: bool = False) -> None:
        """
        Mark a lesson as used (injected into an agent).

        Args:
            lesson_id: Lesson that was used
            was_successful: Whether the trade was successful (for learning)
        """
        try:
            record = self._session.query(LessonRecord).filter_by(lesson_id=lesson_id).first()
            if record:
                record.use_count += 1
                record.last_used_at = datetime.now(UTC)

                if was_successful:
                    record.success_count += 1
                    # Boost score for successful lessons
                    record.current_score = min(record.current_score * 1.1, record.base_score * 2)

                self._session.commit()
        except Exception as e:
            logger.error(f"Failed to mark lesson used: {e}")
            self._session.rollback()

    def delete_lesson(self, lesson_id: str) -> bool:
        """Delete a lesson from memory."""
        try:
            record = self._session.query(LessonRecord).filter_by(lesson_id=lesson_id).first()
            if record:
                self._session.delete(record)
                self._session.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to delete lesson: {e}")
            self._session.rollback()
        return False

    def cleanup_expired(self) -> int:
        """Remove expired and low-relevance lessons."""
        try:
            # Delete expired
            expired = (
                self._session.query(LessonRecord)
                .filter(LessonRecord.expires_at < datetime.now(UTC))
                .delete()
            )

            # Delete very low score lessons
            low_score = (
                self._session.query(LessonRecord)
                .filter(LessonRecord.current_score < self.min_score)
                .delete()
            )

            self._session.commit()
            total = expired + low_score
            logger.info(f"Cleaned up {total} lessons ({expired} expired, {low_score} low score)")
            return total

        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
            self._session.rollback()
            return 0

    def _apply_decay(self) -> None:
        """Apply time-based decay to lesson scores."""
        try:
            records = self._session.query(LessonRecord).all()
            now = datetime.now(UTC)

            for record in records:
                # Handle both timezone-naive and timezone-aware created_at
                created_at = record.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)

                age_days = (now - created_at).days

                if age_days > self.decay_days:
                    record.current_score = decayed_score(
                        record.base_score, age_days, self.decay_days
                    )

            self._session.commit()

        except Exception as e:
            logger.error(f"Decay application failed: {e}")
            self._session.rollback()

    def get_memory_stats(self) -> dict[str, Any]:
        """Get statistics about stored memory."""
        try:
            total = self._session.query(LessonRecord).count()
            by_category = {}
            by_severity = {}

            for record in self._session.query(LessonRecord).all():
                by_category[record.category] = by_category.get(record.category, 0) + 1
                by_severity[record.severity] = by_severity.get(record.severity, 0) + 1

            return {
                "total_lessons": total,
                "by_category": by_category,
                "by_severity": by_severity,
            }
        except Exception as e:
            logger.error(f"Failed to get stats: {e}")
            return {}

    def _record_to_dict(self, record: LessonRecord) -> dict[str, Any]:
        """Convert record to dictionary."""
        return {
            "lesson_id": record.lesson_id,
            "category": record.category,
            "severity": record.severity,
            "description": record.description,
            "lesson": record.lesson,
            "strategy": record.strategy,
            "regime": record.regime,
            "symbol": record.symbol,
            "context_factors": record.context_factors.split(",") if record.context_factors else [],
            "trade_id": record.trade_id,
            "current_score": record.current_score,
            "use_count": record.use_count,
            "success_count": record.success_count,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
        }
