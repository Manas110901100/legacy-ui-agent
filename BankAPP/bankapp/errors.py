"""Exception hierarchy for every runtime error state the application can reach."""

from __future__ import annotations


class BankError(Exception):
    """Base class for errors that are shown to the user."""

    title = "Error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ValidationError(BankError):
    """User input failed a business or format rule."""

    title = "Validation Error"

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class NotFoundError(BankError):
    """A requested record does not exist."""

    title = "Record Not Found"


class PermissionDeniedError(BankError):
    """The logged-in role is not allowed to perform the action."""

    title = "Access Denied"


class SessionExpiredError(BankError):
    """The session was idle for longer than the configured timeout."""

    title = "Session Expired"


class ConfirmationRequired(BankError):
    """The action needs an explicit Yes from the user before it can proceed."""

    title = "Confirm"


class TransientError(BankError):
    """A temporary failure that is safe to retry."""

    title = "Temporarily Unavailable"


class AppError(BankError):
    """An unrecoverable application or infrastructure failure."""

    title = "Application Error"
