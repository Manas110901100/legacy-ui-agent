"""Password hashing, roles/permissions and the idle-timeout session."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from bankapp.errors import PermissionDeniedError, SessionExpiredError

PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset(
        {"view", "create", "edit", "deposit", "withdraw", "transfer", "transfer_large", "close"}
    ),
    "teller": frozenset({"view", "create", "edit", "deposit", "withdraw", "transfer"}),
    "viewer": frozenset({"view"}),
}

ACTION_LABELS = {
    "view": "view accounts",
    "create": "open new accounts",
    "edit": "edit customer details",
    "deposit": "make deposits",
    "withdraw": "make withdrawals",
    "transfer": "make transfers",
    "transfer_large": "make transfers above the teller limit",
    "close": "close accounts",
}

_ITERATIONS = 100_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Return (hash_hex, salt_hex) using PBKDF2-HMAC-SHA256."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, password_hash)


@dataclass
class Session:
    username: str
    role: str
    timeout: float
    clock: Callable[[], float] = time.monotonic
    last_activity: float = field(default=0.0)
    expired: bool = False

    def __post_init__(self) -> None:
        self.last_activity = self.clock()

    def touch(self) -> None:
        if not self.expired:
            self.last_activity = self.clock()

    def idle_seconds(self) -> float:
        return self.clock() - self.last_activity

    def is_expired(self) -> bool:
        if not self.expired and self.idle_seconds() > self.timeout:
            self.expired = True
        return self.expired

    def ensure_active(self) -> None:
        if self.is_expired():
            raise SessionExpiredError(
                f"Your session expired after {int(self.timeout)} seconds of inactivity.\n"
                "Please log in again."
            )
        self.touch()

    def can(self, permission: str) -> bool:
        return permission in PERMISSIONS.get(self.role, frozenset())

    def require(self, permission: str) -> None:
        if not self.can(permission):
            label = ACTION_LABELS.get(permission, permission)
            raise PermissionDeniedError(
                f"User '{self.username}' ({self.role}) is not allowed to {label}."
            )
