from __future__ import annotations

from endstone.plugin import Service


class DiscordLinkService(Service):
    api_version = 1

    def __init__(self, plugin) -> None:
        super().__init__()
        self._plugin = plugin

    def is_verified(self, player) -> bool:
        return self._plugin.storage.get_link(str(player.unique_id)) is not None

    def get_discord_id(self, player) -> str | None:
        link = self._plugin.storage.get_link(str(player.unique_id))
        return link.discord_id if link else None

    def sync_player(self, player) -> bool:
        return self._plugin.sync_player(player, notify=False)
