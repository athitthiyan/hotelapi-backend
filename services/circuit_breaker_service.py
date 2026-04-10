"""
Lightweight circuit breaker for payment gateway API calls.

States:
  CLOSED   — normal operation, requests pass through
  OPEN     — gateway is down, fail fast without calling the API
  HALF_OPEN — after recovery timeout, allow one probe request through

Transitions:
  CLOSED  → OPEN       when consecutive failures >= failure_threshold
  OPEN    → HALF_OPEN  when recovery_timeout_seconds have elapsed
  HALF_OPEN → CLOSED   when the probe request succeeds
  HALF_OPEN → OPEN     when the probe request fails
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class GatewayUnavailableError(Exception):
    """Raised when the circuit breaker is open and blocking requests."""

    def __init__(self, gateway: str):
        self.gateway = gateway
        super().__init__(
            f"{gateway} payment gateway is temporarily unavailable. Please try again shortly."
        )


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self.recovery_timeout_seconds:
                    self._state = CircuitState.HALF_OPEN
                    logger.info("Circuit breaker '%s' entering HALF_OPEN state (probing)", self.name)
            return self._state

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        current_state = self.state

        if current_state == CircuitState.OPEN:
            logger.warning("Circuit breaker '%s' is OPEN — rejecting call", self.name)
            raise GatewayUnavailableError(self.name)

        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise

        self._record_success()
        return result

    def _record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit breaker '%s' tripped OPEN after %d consecutive failures",
                    self.name,
                    self._failure_count,
                )

    def _record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info("Circuit breaker '%s' recovered — back to CLOSED", self.name)
            self._failure_count = 0
            self._state = CircuitState.CLOSED

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = 0.0


stripe_breaker = CircuitBreaker("Stripe", failure_threshold=5, recovery_timeout_seconds=30)
razorpay_breaker = CircuitBreaker("Razorpay", failure_threshold=5, recovery_timeout_seconds=30)
