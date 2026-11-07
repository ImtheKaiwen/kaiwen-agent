from __future__ import annotations

import asyncio

_TRANSIENT_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "PoolTimeout",
        "ReadTimeout",
        "RemoteProtocolError",
        "TimeoutException",
        "WriteTimeout",
    }
)
_TRANSIENT_STATUS = frozenset({408, 500, 502, 503, 504, 529})


def is_transient_provider_error(error: BaseException) -> bool:
    """Classify genuinely transient provider failures without importing any SDK.

    Connection failures, timeouts, and 408, 5xx, and non-quota 429 responses are
    transient. Authentication, validation, permission, quota, and unknown errors are not,
    so failover never hides a defect or a billing problem.
    """

    if isinstance(error, asyncio.CancelledError):
        return False
    if isinstance(error, TimeoutError | ConnectionError):
        return True
    if {cls.__name__ for cls in type(error).__mro__} & _TRANSIENT_NAMES:
        return True
    status = getattr(error, "status_code", None)
    if not isinstance(status, int):
        status = getattr(error, "code", None)
    if not isinstance(status, int):
        return False
    if status == 429:
        return not _quota_exhausted(error)
    return status in _TRANSIENT_STATUS


def _quota_exhausted(error: BaseException) -> bool:
    code = getattr(error, "code", None)
    return isinstance(code, str) and "quota" in code.lower()
