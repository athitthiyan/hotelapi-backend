"""Tests for the circuit breaker service."""

import time

from services.circuit_breaker_service import (
    CircuitBreaker,
    CircuitState,
    GatewayUnavailableError,
)


def _make_breaker(**kwargs) -> CircuitBreaker:
    defaults = {"name": "TestGateway", "failure_threshold": 3, "recovery_timeout_seconds": 0.2}
    defaults.update(kwargs)
    return CircuitBreaker(**defaults)


class TestCircuitBreakerStates:
    def test_starts_in_closed_state(self):
        cb = _make_breaker()
        assert cb.state == CircuitState.CLOSED

    def test_stays_closed_on_success(self):
        cb = _make_breaker()
        result = cb.call(lambda: "ok")
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED

    def test_stays_closed_below_threshold(self):
        cb = _make_breaker(failure_threshold=3)
        for _ in range(2):
            try:
                cb.call(_raise_error)
            except RuntimeError:
                pass
        assert cb.state == CircuitState.CLOSED

    def test_trips_open_after_threshold_failures(self):
        cb = _make_breaker(failure_threshold=3)
        for _ in range(3):
            try:
                cb.call(_raise_error)
            except RuntimeError:
                pass
        assert cb.state == CircuitState.OPEN

    def test_open_state_rejects_calls_immediately(self):
        cb = _make_breaker(failure_threshold=1)
        try:
            cb.call(_raise_error)
        except RuntimeError:
            pass
        assert cb.state == CircuitState.OPEN

        try:
            cb.call(lambda: "should not run")
            assert False, "Expected GatewayUnavailableError"
        except GatewayUnavailableError as exc:
            assert "TestGateway" in str(exc)

    def test_transitions_to_half_open_after_timeout(self):
        cb = _make_breaker(failure_threshold=1, recovery_timeout_seconds=0.1)
        try:
            cb.call(_raise_error)
        except RuntimeError:
            pass
        assert cb.state == CircuitState.OPEN
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_success_transitions_to_closed(self):
        cb = _make_breaker(failure_threshold=1, recovery_timeout_seconds=0.1)
        try:
            cb.call(_raise_error)
        except RuntimeError:
            pass
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

        result = cb.call(lambda: "recovered")
        assert result == "recovered"
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_transitions_back_to_open(self):
        cb = _make_breaker(failure_threshold=1, recovery_timeout_seconds=0.1)
        try:
            cb.call(_raise_error)
        except RuntimeError:
            pass
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

        try:
            cb.call(_raise_error)
        except RuntimeError:
            pass
        assert cb.state == CircuitState.OPEN

    def test_success_resets_failure_count(self):
        cb = _make_breaker(failure_threshold=3)
        # 2 failures, then a success, then 2 more failures — should not trip
        for _ in range(2):
            try:
                cb.call(_raise_error)
            except RuntimeError:
                pass
        cb.call(lambda: "ok")
        for _ in range(2):
            try:
                cb.call(_raise_error)
            except RuntimeError:
                pass
        assert cb.state == CircuitState.CLOSED

    def test_reset_clears_state(self):
        cb = _make_breaker(failure_threshold=1)
        try:
            cb.call(_raise_error)
        except RuntimeError:
            pass
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_original_exception_propagates(self):
        cb = _make_breaker()
        try:
            cb.call(_raise_value_error)
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "test value error" in str(exc)

    def test_gateway_unavailable_error_contains_name(self):
        exc = GatewayUnavailableError("Stripe")
        assert exc.gateway == "Stripe"
        assert "Stripe" in str(exc)
        assert "temporarily unavailable" in str(exc)


def _raise_error():
    raise RuntimeError("gateway timeout")


def _raise_value_error():
    raise ValueError("test value error")
