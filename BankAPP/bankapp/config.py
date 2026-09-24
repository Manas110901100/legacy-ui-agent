"""Environment-driven settings, including the fault/latency injection knobs."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    db_path: str = "bankapp.db"
    log_path: str = "bankapp.log"
    session_timeout: int = 300
    latency: tuple[float, float] = (0.0, 0.0)
    fault_rate: float = 0.0


def _parse_latency(raw: str) -> tuple[float, float]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return (0.0, 0.0)
    low = float(parts[0])
    high = float(parts[1]) if len(parts) > 1 else low
    if low < 0 or high < low:
        raise ValueError(f"BANKAPP_LATENCY must be 'min,max' with 0 <= min <= max, got {raw!r}")
    return (low, high)


def load_config() -> Config:
    """Build a Config from BANKAPP_* environment variables."""
    fault_rate = float(os.environ.get("BANKAPP_FAULT_RATE", "0"))
    if not 0.0 <= fault_rate <= 1.0:
        raise ValueError(f"BANKAPP_FAULT_RATE must be between 0 and 1, got {fault_rate}")
    timeout = int(os.environ.get("BANKAPP_SESSION_TIMEOUT", "300"))
    if timeout <= 0:
        raise ValueError(f"BANKAPP_SESSION_TIMEOUT must be positive, got {timeout}")
    return Config(
        db_path=os.environ.get("BANKAPP_DB", "bankapp.db"),
        log_path=os.environ.get("BANKAPP_LOG", "bankapp.log"),
        session_timeout=timeout,
        latency=_parse_latency(os.environ.get("BANKAPP_LATENCY", "")),
        fault_rate=fault_rate,
    )
