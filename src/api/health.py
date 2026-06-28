"""
Health Check Module

Provides system health monitoring for all components.
Useful for monitoring dashboards and alerting.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from src.config import get_settings

logger = logging.getLogger(__name__)


class HealthStatus(Enum):
    """Health status levels."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class ServiceHealth:
    """Health status of a single service."""

    name: str
    status: HealthStatus
    latency_ms: float = 0.0
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "latency_ms": round(self.latency_ms, 2),
            "message": self.message,
            "details": self.details,
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass
class SystemHealth:
    """Overall system health."""

    status: HealthStatus
    services: list[ServiceHealth]
    uptime_seconds: float
    version: str = "2.0.0"
    checked_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "version": self.version,
            "uptime_seconds": round(self.uptime_seconds, 2),
            "services": [s.to_dict() for s in self.services],
            "checked_at": self.checked_at.isoformat(),
        }


# Track system start time
_system_start_time = time.time()


async def check_database() -> ServiceHealth:
    """Check database connectivity."""
    start = time.time()

    try:
        from sqlalchemy import create_engine, text

        settings = get_settings()

        engine = create_engine(settings.database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        latency = (time.time() - start) * 1000
        return ServiceHealth(
            name="database",
            status=HealthStatus.HEALTHY,
            latency_ms=latency,
            message="Connected to PostgreSQL",
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return ServiceHealth(
            name="database",
            status=HealthStatus.UNHEALTHY,
            latency_ms=latency,
            message=f"Database error: {str(e)[:100]}",
        )


async def check_groq_api() -> ServiceHealth:
    """Check Groq API connectivity."""
    start = time.time()

    try:
        from langchain_core.messages import HumanMessage
        from langchain_groq import ChatGroq

        settings = get_settings()

        # Simple ping to Groq
        llm = ChatGroq(
            api_key=settings.groq_api_key.get_secret_value(),
            model_name=settings.groq_model_fallback,  # Use smaller model
            temperature=0,
            max_tokens=5,
        )

        response = llm.invoke([HumanMessage(content="Say OK")])

        latency = (time.time() - start) * 1000
        return ServiceHealth(
            name="groq_api",
            status=HealthStatus.HEALTHY,
            latency_ms=latency,
            message="Groq API responding",
            details={"model": settings.groq_model_fallback},
        )
    except Exception as e:
        latency = (time.time() - start) * 1000

        # Check if rate limited
        if "rate_limit" in str(e).lower() or "429" in str(e):
            return ServiceHealth(
                name="groq_api",
                status=HealthStatus.DEGRADED,
                latency_ms=latency,
                message="Groq API rate limited",
            )

        return ServiceHealth(
            name="groq_api",
            status=HealthStatus.UNHEALTHY,
            latency_ms=latency,
            message=f"Groq API error: {str(e)[:100]}",
        )


async def check_market_data() -> ServiceHealth:
    """Check market data feed."""
    start = time.time()

    try:
        import yfinance as yf

        # Quick check with a known symbol
        ticker = yf.Ticker("RELIANCE.NS")
        info = ticker.fast_info

        latency = (time.time() - start) * 1000

        if info and hasattr(info, "last_price"):
            return ServiceHealth(
                name="market_data",
                status=HealthStatus.HEALTHY,
                latency_ms=latency,
                message="Yahoo Finance responding",
                details={"source": "yfinance"},
            )
        else:
            return ServiceHealth(
                name="market_data",
                status=HealthStatus.DEGRADED,
                latency_ms=latency,
                message="Yahoo Finance partial response",
            )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return ServiceHealth(
            name="market_data",
            status=HealthStatus.UNHEALTHY,
            latency_ms=latency,
            message=f"Market data error: {str(e)[:100]}",
        )


async def check_memory_system() -> ServiceHealth:
    """Check agent memory system."""
    start = time.time()

    try:
        from src.memory.database import AgentMemoryDB

        memory_db = AgentMemoryDB()
        lessons = memory_db.get_lessons(limit=1)

        latency = (time.time() - start) * 1000
        return ServiceHealth(
            name="memory_system",
            status=HealthStatus.HEALTHY,
            latency_ms=latency,
            message="Memory database accessible",
            details={"lessons_available": len(lessons) > 0},
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return ServiceHealth(
            name="memory_system",
            status=HealthStatus.DEGRADED,
            latency_ms=latency,
            message=f"Memory system fallback: {str(e)[:100]}",
        )


async def check_circuit_breakers() -> ServiceHealth:
    """Check circuit breaker states."""
    start = time.time()

    try:
        from src.utils.circuit_breaker import get_all_circuit_breaker_stats

        stats = get_all_circuit_breaker_stats()

        open_breakers = [name for name, s in stats.items() if s["state"] == "open"]

        latency = (time.time() - start) * 1000

        if open_breakers:
            return ServiceHealth(
                name="circuit_breakers",
                status=HealthStatus.DEGRADED,
                latency_ms=latency,
                message=f"Open breakers: {', '.join(open_breakers)}",
                details=stats,
            )

        return ServiceHealth(
            name="circuit_breakers",
            status=HealthStatus.HEALTHY,
            latency_ms=latency,
            message="All circuit breakers closed",
            details={"count": len(stats)},
        )
    except Exception:
        latency = (time.time() - start) * 1000
        return ServiceHealth(
            name="circuit_breakers",
            status=HealthStatus.HEALTHY,
            latency_ms=latency,
            message="No circuit breakers configured",
        )


async def check_paper_wallet() -> ServiceHealth:
    """Check paper trading wallet state."""
    start = time.time()

    try:
        from src.execution.paper_engine import LocalPaperEngine

        engine = LocalPaperEngine()

        latency = (time.time() - start) * 1000
        return ServiceHealth(
            name="paper_wallet",
            status=HealthStatus.HEALTHY,
            latency_ms=latency,
            message=f"Balance: ₹{engine.balance:,.2f}",
            details={
                "balance": engine.balance,
                "positions": len(engine.positions),
                "total_trades": engine.total_trades,
            },
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return ServiceHealth(
            name="paper_wallet",
            status=HealthStatus.UNHEALTHY,
            latency_ms=latency,
            message=f"Paper wallet error: {str(e)[:100]}",
        )


async def health_check(
    include_slow_checks: bool = False,
) -> SystemHealth:
    """
    Perform comprehensive system health check.

    Args:
        include_slow_checks: Include checks that may be slow (Groq API)

    Returns:
        SystemHealth with all service statuses
    """
    # Fast checks - always run
    checks = [
        check_memory_system(),
        check_circuit_breakers(),
        check_paper_wallet(),
    ]

    # Slow checks - optional
    if include_slow_checks:
        checks.extend(
            [
                check_database(),
                check_groq_api(),
                check_market_data(),
            ]
        )

    # Run all checks concurrently
    services = await asyncio.gather(*checks, return_exceptions=True)

    # Convert exceptions to unhealthy status
    processed_services = []
    for service in services:
        if isinstance(service, Exception):
            processed_services.append(
                ServiceHealth(
                    name="unknown",
                    status=HealthStatus.UNHEALTHY,
                    message=str(service),
                )
            )
        else:
            processed_services.append(service)

    # Determine overall status
    statuses = [s.status for s in processed_services]

    if any(s == HealthStatus.UNHEALTHY for s in statuses):
        overall_status = HealthStatus.UNHEALTHY
    elif any(s == HealthStatus.DEGRADED for s in statuses):
        overall_status = HealthStatus.DEGRADED
    else:
        overall_status = HealthStatus.HEALTHY

    uptime = time.time() - _system_start_time

    return SystemHealth(
        status=overall_status,
        services=processed_services,
        uptime_seconds=uptime,
    )


async def quick_health_check() -> dict[str, str]:
    """Quick health check for load balancers."""
    return {
        "status": "ok",
        "uptime": f"{time.time() - _system_start_time:.0f}s",
    }
