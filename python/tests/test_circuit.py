import pytest

from cubiczan_resilience import CircuitBreaker, CircuitOpenError, CircuitState


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_opens_after_threshold():
    clock = FakeClock()
    cb = CircuitBreaker("x", failure_threshold=3, cooldown_seconds=10, clock=clock)
    assert cb.state is CircuitState.CLOSED
    cb.record_failure()
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    assert cb.allow() is False


def test_half_opens_after_cooldown_then_closes_on_success():
    clock = FakeClock()
    cb = CircuitBreaker("x", failure_threshold=1, cooldown_seconds=10, clock=clock)
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    assert cb.allow() is False

    clock.advance(9.9)
    assert cb.state is CircuitState.OPEN  # cooldown not elapsed

    clock.advance(0.2)  # total 10.1
    assert cb.state is CircuitState.HALF_OPEN
    assert cb.allow() is True  # one trial slot
    assert cb.allow() is False  # slot consumed

    cb.record_success()
    assert cb.state is CircuitState.CLOSED


def test_half_open_failure_reopens():
    clock = FakeClock()
    cb = CircuitBreaker("x", failure_threshold=1, cooldown_seconds=5, clock=clock)
    cb.record_failure()
    clock.advance(6)
    assert cb.state is CircuitState.HALF_OPEN
    cb.record_failure()
    assert cb.state is CircuitState.OPEN


def test_guard_raises_when_open():
    clock = FakeClock()
    cb = CircuitBreaker("x", failure_threshold=1, cooldown_seconds=5, clock=clock)
    cb.record_failure()
    with pytest.raises(CircuitOpenError):
        cb.guard()
    assert cb.retry_after() > 0


def test_reset():
    cb = CircuitBreaker("x", failure_threshold=1)
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    cb.reset()
    assert cb.state is CircuitState.CLOSED
    assert cb.failure_count == 0
