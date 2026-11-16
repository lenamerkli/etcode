"""Exceptions raised by the backend client.

The hierarchy lets callers react to the cases that matter without parsing messages:

* :class:`Unreachable`: no usable answer (connection refused, DNS, timeout, dropped stream).
* :class:`BackendError`: the backend answered with an error. ``status`` and ``code``
  come from its ``{"error": {"code", "message"}}`` body. Subclasses: :class:`AuthError`
  (401), :class:`NotFound` (404), :class:`Conflict` (409), :class:`ServerError` (5xx).
* :class:`ProtocolError`: the answer made no sense (not JSON, a field is missing).
"""
from __future__ import annotations


class EtcodeError(Exception):
    """Base class of everything this package raises on purpose."""


class Unreachable(EtcodeError):
    """The backend could not be reached, or the connection broke."""


class ProtocolError(EtcodeError):
    """The backend answered, but not in the documented format."""


class BackendError(EtcodeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(status, code, message)
        self.status = status
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.message} ({self.code}, HTTP {self.status})"


class AuthError(BackendError):
    """401: the token is missing or wrong."""


class NotFound(BackendError):
    """404: for example an unknown session, or a session that belongs to another token."""


class Conflict(BackendError):
    """409: for example 'nothing to cancel', or a tool result for a call that is already resolved."""


class ServerError(BackendError):
    """5xx."""


class Unsupported(BackendError):
    """The backend is older than the client and lacks an endpoint (see ``BackendClient.list_sessions``)."""
