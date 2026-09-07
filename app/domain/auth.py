from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


@dataclass
class User:
    id: str
    display_name: str
    avatar_url: str | None
    role: UserRole
    active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Principal:
    user_id: str
    display_name: str
    avatar_url: str | None
    role: UserRole
    session_id: str

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN


@dataclass(frozen=True)
class ExternalIdentity:
    id: str
    user_id: str
    provider: str
    subject: str
    created_at: datetime


@dataclass(frozen=True)
class FederatedProfile:
    subject: str
    display_name: str
    avatar_url: str | None


class RefreshRotationStatus(StrEnum):
    ROTATED = "rotated"
    REPLAYED = "replayed"
    INVALID = "invalid"


@dataclass(frozen=True)
class RefreshRotation:
    status: RefreshRotationStatus
    user_id: str | None = None
    generation: int | None = None
