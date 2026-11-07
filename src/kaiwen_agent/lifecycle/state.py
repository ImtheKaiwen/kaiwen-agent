from __future__ import annotations

from kaiwen_agent.lifecycle.models import LifecycleState


class InvalidLifecycleTransition(ValueError):
    pass


_ALLOWED_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.STOPPED: frozenset({LifecycleState.STARTING}),
    LifecycleState.STARTING: frozenset(
        {LifecycleState.SLEEPING, LifecycleState.DEGRADED, LifecycleState.FAILED}
    ),
    LifecycleState.SLEEPING: frozenset(
        {LifecycleState.WAKING, LifecycleState.PAUSED, LifecycleState.STOPPED}
    ),
    LifecycleState.WAKING: frozenset(
        {LifecycleState.OBSERVING, LifecycleState.PAUSED, LifecycleState.FAILED}
    ),
    LifecycleState.OBSERVING: frozenset(
        {
            LifecycleState.THINKING,
            LifecycleState.ACTING,
            LifecycleState.PERSISTING,
            LifecycleState.PAUSED,
            LifecycleState.FAILED,
        }
    ),
    LifecycleState.THINKING: frozenset(
        {
            LifecycleState.ACTING,
            LifecycleState.PERSISTING,
            LifecycleState.WAITING_APPROVAL,
            LifecycleState.PAUSED,
            LifecycleState.FAILED,
        }
    ),
    LifecycleState.ACTING: frozenset(
        {
            LifecycleState.PERSISTING,
            LifecycleState.WAITING_APPROVAL,
            LifecycleState.WAITING_JOB,
            LifecycleState.PAUSED,
            LifecycleState.FAILED,
        }
    ),
    LifecycleState.PERSISTING: frozenset(
        {LifecycleState.SCHEDULING, LifecycleState.PAUSED, LifecycleState.FAILED}
    ),
    LifecycleState.SCHEDULING: frozenset(
        {LifecycleState.SLEEPING, LifecycleState.PAUSED, LifecycleState.FAILED}
    ),
    LifecycleState.WAITING_APPROVAL: frozenset(
        {LifecycleState.WAKING, LifecycleState.PAUSED, LifecycleState.FAILED}
    ),
    LifecycleState.WAITING_JOB: frozenset(
        {LifecycleState.WAKING, LifecycleState.PAUSED, LifecycleState.FAILED}
    ),
    LifecycleState.PAUSED: frozenset(
        {
            LifecycleState.SLEEPING,
            LifecycleState.WAKING,
            LifecycleState.WAITING_APPROVAL,
            LifecycleState.WAITING_JOB,
            LifecycleState.STOPPED,
        }
    ),
    LifecycleState.DEGRADED: frozenset(
        {LifecycleState.STARTING, LifecycleState.PAUSED, LifecycleState.STOPPED}
    ),
    LifecycleState.FAILED: frozenset(
        {LifecycleState.STARTING, LifecycleState.PAUSED, LifecycleState.STOPPED}
    ),
}


def validate_lifecycle_transition(current: LifecycleState, target: LifecycleState) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidLifecycleTransition(f"Cannot transition lifecycle {current} -> {target}")
