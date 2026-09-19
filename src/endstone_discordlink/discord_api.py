from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

class DiscordApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Erreur API Discord {status} : {message}")
        self.status = status
        self.message = message

@dataclass(frozen=True)
class SyncResult:
    added: tuple[str, ...]
    removed: tuple[str, ...]

class DiscordClient:
    API = "https://discord.com/api/v10"

    def __init__(self, token: str, guild_id: str, timeout: float = 8.0) -> None:
        self.token = token.strip()
        self.guild_id = guild_id.strip()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self.guild_id)

    def test(self) -> dict:
        return self._request("GET", "/users/@me")

    def get_application(self) -> dict:
        return self._request("GET", "/oauth2/applications/@me")

    def get_gateway_bot(self) -> dict:
        return self._request("GET", "/gateway/bot")

    def get_member(self, user_id: str) -> dict:
        return self._request("GET", f"/guilds/{self.guild_id}/members/{user_id}")

    def send_components_message(self, channel_id: str, components: list[dict]) -> dict:
        return self._request(
            "POST",
            f"/channels/{channel_id}/messages",
            {"flags": 32768, "components": components},
        )

    def send_dm_message(self, user_id: str, content: str) -> dict:
        channel = self._request("POST", "/users/@me/channels", {"recipient_id": user_id})
        channel_id = str(channel.get("id", "")).strip()
        if not channel_id:
            raise DiscordApiError(0, "Discord n'a pas retourné de canal de messages privés")
        return self._request("POST", f"/channels/{channel_id}/messages", {"content": content})

    def interaction_callback(self, interaction_id: str, interaction_token: str, payload: dict) -> None:
        self._request(
            "POST",
            f"/interactions/{interaction_id}/{interaction_token}/callback",
            payload,
            no_content=True,
            use_auth=False,
        )

    def edit_original_interaction(self, application_id: str, interaction_token: str, payload: dict) -> dict:
        return self._request(
            "PATCH",
            f"/webhooks/{application_id}/{interaction_token}/messages/@original",
            payload,
            use_auth=False,
        )

    def upsert_guild_command(self, application_id: str, payload: dict) -> dict:
        return self._request(
            "POST",
            f"/applications/{application_id}/guilds/{self.guild_id}/commands",
            payload,
        )

    def sync_roles(
        self,
        user_id: str,
        *,
        desired: set[str],
        managed: set[str],
        remove_managed: bool,
    ) -> SyncResult:
        member = self.get_member(user_id)
        current = {str(role) for role in member.get("roles", [])}
        desired = {role for role in desired if role}
        managed = {role for role in managed if role}
        to_add = sorted(desired - current)
        to_remove = sorted((current & managed) - desired) if remove_managed else []
        for role_id in to_add:
            self._request(
                "PUT",
                f"/guilds/{self.guild_id}/members/{user_id}/roles/{role_id}",
                no_content=True,
            )
        for role_id in to_remove:
            self._request(
                "DELETE",
                f"/guilds/{self.guild_id}/members/{user_id}/roles/{role_id}",
                no_content=True,
            )
        return SyncResult(tuple(to_add), tuple(to_remove))

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        no_content: bool = False,
        use_auth: bool = True,
    ):
        if use_auth and not self.token:
            raise DiscordApiError(0, "Le jeton du bot n'est pas configuré")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "EndstoneDiscordLink/2.2.3 (+https://endstone.dev)",
        }
        if use_auth:
            headers["Authorization"] = f"Bot {self.token}"
        request = urllib.request.Request(
            self.API + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                if no_content or not body:
                    return {}
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
                message = parsed.get("message", body)
                retry_after = parsed.get("retry_after")
                if exc.code == 429 and retry_after:
                    time.sleep(min(float(retry_after), 3.0))
            except Exception:
                message = body or str(exc)
            raise DiscordApiError(int(exc.code), str(message)) from exc
        except urllib.error.URLError as exc:
            raise DiscordApiError(0, str(exc.reason)) from exc
