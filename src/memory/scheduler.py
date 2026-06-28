"""
Memory Decay Scheduler Module

Handles automatic decay of lesson relevance over time
and cleanup of low-value lessons.
"""

import asyncio
import logging
from datetime import datetime, timedelta, UTC
from typing import Any

from .database import AgentMemoryDB

logger = logging.getLogger(__name__)


class MemoryDecayScheduler:
    """
    Scheduler for memory maintenance tasks.
    
    Responsibilities:
    - Decay lesson scores over time
    - Prune low-relevance lessons
    - Track lesson usage statistics
    """
    
    def __init__(
        self,
        memory_db: AgentMemoryDB | None = None,
        decay_interval_hours: int = 24,
        min_score_threshold: float = 0.1,
    ):
        """
        Initialize the scheduler.
        
        Args:
            memory_db: Memory database instance
            decay_interval_hours: Hours between decay runs
            min_score_threshold: Minimum score before pruning
        """
        self.memory_db = memory_db or AgentMemoryDB()
        self.decay_interval = timedelta(hours=decay_interval_hours)
        self.min_score_threshold = min_score_threshold
        
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_decay_run: datetime | None = None
    
    def apply_time_decay(self) -> dict[str, Any]:
        """
        Apply time-based decay to all lessons.
        
        Lessons older than the decay period have their scores reduced.
        
        Returns:
            Statistics about the decay operation
        """
        try:
            session = self.memory_db._session
            if session is None:
                logger.warning("No database session available")
                return {"status": "error", "message": "No database session"}
            
            from .database import LessonRecord, decayed_score

            now = datetime.now(UTC)
            decay_threshold = now - timedelta(days=self.memory_db.decay_days)

            # Get lessons older than decay threshold
            old_lessons = session.query(LessonRecord).filter(
                LessonRecord.created_at < decay_threshold
            ).all()

            decayed_count = 0
            for lesson in old_lessons:
                age_days = (now - lesson.created_at.replace(tzinfo=UTC)).days
                # Use the shared decay formula (single source of truth in database.py)
                # so the scheduler and on-read decay never disagree.
                new_score = decayed_score(lesson.base_score, age_days, self.memory_db.decay_days)

                if new_score != lesson.current_score:
                    lesson.current_score = new_score
                    decayed_count += 1
            
            session.commit()
            self._last_decay_run = now
            
            logger.info(f"Decay applied to {decayed_count} lessons")
            return {
                "status": "success",
                "lessons_processed": len(old_lessons),
                "lessons_decayed": decayed_count,
                "timestamp": now.isoformat(),
            }
            
        except Exception as e:
            logger.error(f"Error applying decay: {e}")
            return {"status": "error", "message": str(e)}
    
    def prune_low_score_lessons(self) -> dict[str, Any]:
        """
        Remove lessons with scores below threshold.
        
        Returns:
            Statistics about pruning operation
        """
        try:
            session = self.memory_db._session
            if session is None:
                return {"status": "error", "message": "No database session"}
            
            from .database import LessonRecord
            
            # Find lessons below threshold
            low_score_lessons = session.query(LessonRecord).filter(
                LessonRecord.current_score < self.min_score_threshold
            ).all()
            
            pruned_count = 0
            for lesson in low_score_lessons:
                # Don't prune high-severity lessons regardless of score
                if lesson.severity in ["high", "critical"]:
                    continue
                
                # Don't prune lessons used successfully
                if lesson.success_count > 0:
                    continue
                
                session.delete(lesson)
                pruned_count += 1
            
            session.commit()
            
            logger.info(f"Pruned {pruned_count} low-relevance lessons")
            return {
                "status": "success",
                "candidates": len(low_score_lessons),
                "pruned": pruned_count,
            }
            
        except Exception as e:
            logger.error(f"Error pruning lessons: {e}")
            return {"status": "error", "message": str(e)}
    
    def prune_expired_lessons(self) -> dict[str, Any]:
        """
        Remove lessons that have passed their expiry date.
        
        Returns:
            Statistics about pruning operation
        """
        try:
            session = self.memory_db._session
            if session is None:
                return {"status": "error", "message": "No database session"}
            
            from .database import LessonRecord
            
            now = datetime.now(UTC)
            
            # Find expired lessons
            expired_lessons = session.query(LessonRecord).filter(
                LessonRecord.expires_at.isnot(None),
                LessonRecord.expires_at < now
            ).all()
            
            pruned_count = len(expired_lessons)
            for lesson in expired_lessons:
                session.delete(lesson)
            
            session.commit()
            
            logger.info(f"Pruned {pruned_count} expired lessons")
            return {
                "status": "success",
                "pruned": pruned_count,
            }
            
        except Exception as e:
            logger.error(f"Error pruning expired lessons: {e}")
            return {"status": "error", "message": str(e)}
    
    def boost_successful_lessons(self, boost_factor: float = 1.1) -> dict[str, Any]:
        """
        Boost scores of lessons that have been successfully applied.
        
        Args:
            boost_factor: Multiplier for successful lessons
            
        Returns:
            Statistics about boosting operation
        """
        try:
            session = self.memory_db._session
            if session is None:
                return {"status": "error", "message": "No database session"}
            
            from .database import LessonRecord
            
            # Find lessons with high success rate
            successful_lessons = session.query(LessonRecord).filter(
                LessonRecord.success_count > 0,
                LessonRecord.use_count > 0
            ).all()
            
            boosted_count = 0
            for lesson in successful_lessons:
                success_rate = lesson.success_count / lesson.use_count
                
                # Boost if success rate > 50%
                if success_rate > 0.5:
                    new_score = lesson.current_score * boost_factor
                    # Cap at 2x base score
                    new_score = min(new_score, lesson.base_score * 2.0)
                    
                    if new_score != lesson.current_score:
                        lesson.current_score = new_score
                        boosted_count += 1
            
            session.commit()
            
            logger.info(f"Boosted {boosted_count} successful lessons")
            return {
                "status": "success",
                "candidates": len(successful_lessons),
                "boosted": boosted_count,
            }
            
        except Exception as e:
            logger.error(f"Error boosting lessons: {e}")
            return {"status": "error", "message": str(e)}
    
    async def run_maintenance(self) -> dict[str, Any]:
        """
        Run all maintenance tasks.
        
        Returns:
            Combined statistics from all tasks
        """
        results = {
            "timestamp": datetime.now().isoformat(),
            "tasks": {},
        }
        
        # Apply decay
        results["tasks"]["decay"] = self.apply_time_decay()
        
        # Prune expired
        results["tasks"]["expire"] = self.prune_expired_lessons()
        
        # Prune low score
        results["tasks"]["prune"] = self.prune_low_score_lessons()
        
        # Boost successful
        results["tasks"]["boost"] = self.boost_successful_lessons()
        
        return results
    
    async def start(self) -> None:
        """Start the scheduled maintenance loop."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._maintenance_loop())
        logger.info("Memory decay scheduler started")
    
    async def stop(self) -> None:
        """Stop the scheduled maintenance loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Memory decay scheduler stopped")
    
    async def _maintenance_loop(self) -> None:
        """Background loop for scheduled maintenance."""
        while self._running:
            try:
                # Run maintenance
                results = await self.run_maintenance()
                logger.info(f"Maintenance completed: {results}")
                
                # Wait for next interval
                await asyncio.sleep(self.decay_interval.total_seconds())
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Maintenance loop error: {e}")
                # Wait a bit before retrying
                await asyncio.sleep(60)


# ===========================================
# Convenience Functions
# ===========================================

_scheduler: MemoryDecayScheduler | None = None


def get_memory_scheduler() -> MemoryDecayScheduler:
    """Get or create the global memory scheduler."""
    global _scheduler
    if _scheduler is None:
        _scheduler = MemoryDecayScheduler()
    return _scheduler


async def run_memory_maintenance() -> dict[str, Any]:
    """Run memory maintenance tasks immediately."""
    scheduler = get_memory_scheduler()
    return await scheduler.run_maintenance()
