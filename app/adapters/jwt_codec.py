from collections.abc import Callable
from datetime import datetime, timedelta
from math import isfinite
from uuid import uuid4

import jwt

from app.domain.auth import Principal, User, UserRole
from app.domain.errors import AuthenticationFailed


class JWTAccessTokenCodec:
    def __init__(
        self,
        secret: str,
        issuer: str,
        audience: str,
        lifetime_seconds: int = 900,
        jti_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes")
        self.secret = secret
        self.issuer = issuer
        self.audience = audience
        self.lifetime_seconds = lifetime_seconds
        self.jti_factory = jti_factory

    def encode(self, user: User, session_id: str, now: datetime) -> str:
        return jwt.encode(
            {
                "iss": self.issuer,
                "aud": self.audience,
                "sub": user.id,
                "role": user.role.value,
                "sid": session_id,
                "iat": now,
                "exp": now + timedelta(seconds=self.lifetime_seconds),
                "jti": self.jti_factory(),
            },
            self.secret,
            algorithm="HS256",
        )

    def decode(self, token: str, now: datetime) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self.secret,
                algorithms=["HS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={
                    "require": ["iss", "aud", "sub", "role", "sid", "iat", "exp", "jti"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
            user_id = claims["sub"]
            session_id = claims["sid"]
            role = UserRole(claims["role"])
            issued_at = claims["iat"]
            expires_at = claims["exp"]
            if not isinstance(user_id, str) or not user_id:
                raise ValueError("Invalid subject")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("Invalid session")
            if (
                isinstance(issued_at, bool)
                or not isinstance(issued_at, int | float)
                or isinstance(expires_at, bool)
                or not isinstance(expires_at, int | float)
            ):
                raise ValueError("Invalid temporal claim")
            try:
                finite_temporal_claims = isfinite(issued_at) and isfinite(expires_at)
            except OverflowError:
                finite_temporal_claims = False
            if not finite_temporal_claims:
                raise ValueError("Invalid temporal claim")
            current_time = now.timestamp()
            if issued_at > current_time or expires_at <= current_time:
                raise ValueError("Access token is outside its validity window")
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
            raise AuthenticationFailed("Invalid access token") from error
        return Principal(user_id, "", None, role, session_id)
