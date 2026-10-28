"""Public API errors."""
from __future__ import annotations


class APIError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class ProviderError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
