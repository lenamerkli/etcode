"""Sentences for the user. They never contain the token."""
from __future__ import annotations

from .errors import AuthError, BackendError, ProtocolError, Unreachable
from .store import StoreError


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, AuthError):
        return "The backend rejected the access token (HTTP 401). Check the token in the connection settings."
    if isinstance(exc, Unreachable):
        return str(exc)
    if isinstance(exc, ProtocolError):
        return f"Unexpected reply from the backend: {exc}"
    if isinstance(exc, BackendError):
        return f"{exc.message} ({exc.code})"
    if isinstance(exc, (StoreError, ValueError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"
