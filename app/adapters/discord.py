from urllib.parse import urlencode

import httpx

from app.domain.auth import FederatedProfile
from app.domain.errors import ProviderAuthenticationError


class DiscordIdentityProvider:
    id = "discord"
    display_name = "Discord"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.transport = transport
        self.timeout = timeout

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "scope": "identify",
                "state": state,
                "redirect_uri": redirect_uri,
            }
        )
        return f"https://discord.com/oauth2/authorize?{query}"

    async def authenticate(self, code: str, redirect_uri: str) -> FederatedProfile:
        try:
            async with httpx.AsyncClient(
                base_url="https://discord.com", transport=self.transport, timeout=self.timeout
            ) as client:
                token_response = await client.post(
                    "/api/v10/oauth2/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
                token_response.raise_for_status()
                token_payload = token_response.json()
                access_token = (
                    token_payload.get("access_token") if isinstance(token_payload, dict) else None
                )
                if not isinstance(access_token, str) or not access_token:
                    raise ValueError("Provider token response did not include an access token")
                profile_response = await client.get(
                    "/api/v10/users/@me",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                profile_response.raise_for_status()
                payload = profile_response.json()
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise ProviderAuthenticationError("Discord authentication failed") from error

        subject = payload.get("id") if isinstance(payload, dict) else None
        username = payload.get("username") if isinstance(payload, dict) else None
        global_name = payload.get("global_name") if isinstance(payload, dict) else None
        display_name = global_name or username
        if not isinstance(subject, str) or not subject:
            raise ProviderAuthenticationError("Discord profile did not include a user ID")
        if not isinstance(display_name, str) or not display_name:
            raise ProviderAuthenticationError("Discord profile did not include a display name")
        avatar = payload.get("avatar")
        avatar_url = None
        if isinstance(avatar, str) and avatar:
            avatar_url = f"https://cdn.discordapp.com/avatars/{subject}/{avatar}.png"
        return FederatedProfile(subject, display_name, avatar_url)
