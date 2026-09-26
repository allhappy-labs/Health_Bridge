"""Non-secret archive owner claim and state values, plus credential validation."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import re


_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def credential_digest(secret: str) -> str:
    """Hash exactly 32 bytes encoded as canonical unpadded base64url."""
    if not isinstance(secret, str) or not _CREDENTIAL.fullmatch(secret):
        raise ValueError("invalid_credential")
    try:
        raw = base64.urlsafe_b64decode(secret + "=")
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("invalid_credential") from exc
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).decode().rstrip("=") != secret:
        raise ValueError("invalid_credential")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class OwnerClaim:
    claim_id: str
    fingerprint: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OwnerState:
    state: str
    generation: int
    claim_id: str | None = None
    fingerprint: str | None = None
    expires_at: datetime | None = None
