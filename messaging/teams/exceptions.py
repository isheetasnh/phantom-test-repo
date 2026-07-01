from typing import Any


class TeamsConfigError(RuntimeError):
    """Raised when the Teams POC is missing destination or token config."""


class TeamsAPIError(RuntimeError):
    """Raised when Microsoft Graph returns a non-success response."""

    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(
            f"Microsoft Graph request failed: status={status} body={payload!r}"
        )
