import secrets
import shutil
import string
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

from endstone import Player
from endstone.command import Command, CommandSender
from endstone.event import EventPriority, PlayerJoinEvent, PluginEnableEvent, event_handler
from endstone.plugin import Plugin, ServicePriority

from .discord_api import DiscordApiError, DiscordClient
from .discord_gateway import DiscordGateway
from .discord_ui import DiscordInteractionController
from .forms import DiscordForms
from .integrations import IntegrationResolver, IntegrationSnapshot
from .service import DiscordLinkService
from .storage import LinkRecord, LinkStorage

PREFIX = "§9§lDiscordLink§r §8»§r"
SERVICE_NAME = "discordlink.links.v1"


@dataclass(frozen=True)
class Settings:
    token: str
    guild_id: str
    verification_channel_id: str
    server_name: str
    server_invite_url: str
    require_guild_membership: bool
    restrict_to_verification_channel: bool
    gateway_enabled: bool
    register_commands: bool
    verified_role_id: str
    remove_managed_roles: bool
    sync_interval_seconds: int
    code_length: int
    expiry_seconds: int
    max_attempts: int
    cooldown_seconds: int
    allow_relink: bool
    enable_stoneperms: bool
    enable_pureperms: bool
    enable_factions: bool
    enable_economy: bool


@dataclass(frozen=True)
class ProfileContext:
    primary_group: str
    groups: tuple[str, ...]
    faction: str
    faction_rank: str
    balance: float | None


class DiscordListener:
    def __init__(self, plugin: "DiscordLinkPlugin") -> None:
        self.plugin = plugin

    @event_handler(priority=EventPriority.MONITOR)
    def on_join(self, event: PlayerJoinEvent) -> None:
        player = event.player
        self.plugin.storage.touch_player(str(player.unique_id), _xuid(player), player.name)
        self.plugin.refresh_profile(player)
        self.plugin.server.scheduler.run_task(self.plugin, lambda: self.plugin.sync_player(player, notify=False), delay=40)

    @event_handler(priority=EventPriority.MONITOR)
    def on_plugin_enable(self, event: PluginEnableEvent) -> None:
        if event.plugin is self.plugin:
            return
        self.plugin.server.scheduler.run_task(self.plugin, self.plugin.log_integrations, delay=20)


class DiscordLinkPlugin(Plugin):
    api_version = "0.11"
    version = "2.2.1"
    description = "Discord Components V2 verification and role sync for Endstone"
    authors: ClassVar[list[str]] = ["KyroMC"]
    prefix = "DiscordLink"
    soft_depend: ClassVar[list[str]] = ["stoneperms", "pureperms", "zfactions", "bedrockeconomy", "kyromc"]

    commands: ClassVar[dict[str, Any]] = {
        "link": {
            "description": "Link your Minecraft account to Discord",
            "aliases": ["discord"],
            "usages": [
                "/link",
                "/link status",
                "/link unlink",
                "/link sync",
            ],
            "permissions": ["discordlink.command.link"],
        },
        "discordlinkadmin": {
            "description": "Administer DiscordLink",
            "aliases": ["dlinkadmin", "discordadmin", "dauth"],
            "usages": [
                "/discordlinkadmin",
                "/discordlinkadmin panel",
                "/discordlinkadmin panel <channel_id: string>",
                "/discordlinkadmin status",
                "/discordlinkadmin reload",
                "/discordlinkadmin sync <player: string>",
                "/discordlinkadmin unlink <player: string>",
            ],
            "permissions": ["discordlink.command.admin"],
        },
    }
    permissions: ClassVar[dict[str, Any]] = {
        "discordlink.command.link": {"description": "Use DiscordLink account linking", "default": True},
        "discordlink.command.admin": {"description": "Administer DiscordLink", "default": "op"},
    }

    def __init__(self) -> None:
        super().__init__()
        self.settings = Settings("", "", "", "Minecraft Server", "", True, True, True, True, "", True, 60, 6, 600, 5, 60, False, True, True, True, True)
        self.storage = LinkStorage(Path("discordlink.db"))
        self.discord = DiscordClient("", "")
        self.integrations = IntegrationResolver(self)
        self.forms = DiscordForms(self)
        self.discord_ui = DiscordInteractionController(self)
        self._gateway: DiscordGateway | None = None
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="discordlink")
        self._enabled_runtime = False
        self._service: DiscordLinkService | None = None
        self._profile_cache: dict[str, ProfileContext] = {}
        self._last_periodic_sync = 0.0
        self._runtime_generation = 0


    def _migrate_legacy_data(self) -> None:
        target = Path(self.data_folder)
        target.mkdir(parents=True, exist_ok=True)
        legacy = target.parent / "discordauth"
        target_config = target / "config.toml"
        target_db = target / "discordlink.db"
        legacy_config = legacy / "config.toml"
        legacy_db = legacy / "discordauth.db"
        local_legacy_db = target / "discordauth.db"
        if not target_config.exists() and legacy_config.exists():
            text = legacy_config.read_text(encoding="utf-8").replace("DiscordAuth", "DiscordLink")
            target_config.write_text(text, encoding="utf-8")
            self.logger.info("Migrated DiscordAuth config to DiscordLink")
        if not target_db.exists():
            source = legacy_db if legacy_db.exists() else local_legacy_db
            if source.exists():
                shutil.copy2(source, target_db)
                self.logger.info("Migrated DiscordAuth database to DiscordLink")

    @property
    def gateway_running(self) -> bool:
        return self._gateway is not None and self._gateway.running

    def on_enable(self) -> None:
        self._migrate_legacy_data()
        self.save_default_config()
        self.storage = LinkStorage(Path(self.data_folder) / "discordlink.db")
        self.storage.initialize()
        self.integrations = IntegrationResolver(self)
        self.forms = DiscordForms(self)
        self._load_settings()
        self._enabled_runtime = True
        self.register_events(DiscordListener(self))
        self._service = DiscordLinkService(self)
        self.server.service_manager.register(SERVICE_NAME, self._service, self, ServicePriority.NORMAL)
        self.server.scheduler.run_task(self, self._periodic_tick, delay=20, period=20)
        self.log_integrations()
        self._restart_discord_runtime()

    def on_disable(self) -> None:
        self._enabled_runtime = False
        self._runtime_generation += 1
        if self._gateway is not None:
            self._gateway.stop()
            self._gateway = None
        try:
            self.server.service_manager.unregister_all(self)
        except Exception:
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.logger.info("DiscordLink disabled")

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name == "link":
            return self._link_command(sender, args)
        if command.name == "discordlinkadmin":
            return self._admin_command(sender, args)
        return False

    def _link_command(self, sender: CommandSender, args: list[str]) -> bool:
        if not isinstance(sender, Player):
            sender.send_message(f"{PREFIX} §cThis command must be used in-game.")
            return True
        if not args:
            self.forms.open_player(sender)
            return True
        action = args[0].casefold()
        if action == "status":
            self.send_link_status(sender)
        elif action == "unlink":
            self.unlink_player(sender)
        elif action == "sync":
            self.sync_player(sender, notify=True)
        else:
            sender.send_message(f"{PREFIX} §7Use §f/link §7to open the Discord linking menu.")
        return True

    def _admin_command(self, sender: CommandSender, args: list[str]) -> bool:
        if not args:
            if isinstance(sender, Player):
                self.forms.open_admin(sender)
            else:
                self._send_admin_status(sender)
            return True
        action = args[0].casefold()
        if action == "panel":
            channel_id = args[1].strip() if len(args) >= 2 else None
            self.send_panel_from_minecraft(sender, channel_id)
        elif action == "status":
            self._send_admin_status(sender)
        elif action == "reload":
            self.reload_runtime(sender)
        elif action == "sync" and len(args) >= 2:
            target = self.server.get_player(args[1])
            if target is None:
                sender.send_message(f"{PREFIX} §cThat player must be online to sync provider data.")
            else:
                ok = self.sync_player(target, notify=False)
                sender.send_message(f"{PREFIX} {'§aSync queued.' if ok else '§cPlayer is not linked or Discord is not configured.'}")
        elif action == "unlink" and len(args) >= 2:
            self.admin_unlink(sender, args[1])
        else:
            sender.send_message(f"{PREFIX} §cUnknown admin action.")
        return True

    def start_link(self, player: Player, discord_id: str) -> None:
        discord_id = discord_id.strip()
        if not _valid_discord_id(discord_id):
            player.send_message(f"{PREFIX} §cEnter a valid Discord user ID containing 17-20 digits.")
            return
        if not self.discord.configured:
            player.send_message(f"{PREFIX} §cDiscord linking is not configured by the server owner yet.")
            return
        player_uuid = str(player.unique_id)
        current = self.storage.get_link(player_uuid)
        if current is not None and not self.settings.allow_relink:
            player.send_message(f"{PREFIX} §eYour account is already linked. Unlink it first if you need to change Discord accounts.")
            return
        code = _code(self.settings.code_length)
        pending = self.storage.create_pending(
            player_uuid=player_uuid,
            xuid=_xuid(player),
            player_name=player.name,
            discord_id=discord_id,
            code=code,
            expiry_seconds=self.settings.expiry_seconds,
            cooldown_seconds=self.settings.cooldown_seconds,
        )
        if not pending.ok:
            messages = {
                "discord-in-use": "§cThat Discord account is already linked to another player.",
                "discord-pending": "§cThat Discord account already has an active verification request.",
                "cooldown": "§ePlease wait before generating another verification code.",
            }
            player.send_message(f"{PREFIX} {messages.get(pending.reason, '§cCould not create a verification request.')}")
            return
        player_name = player.name
        player.send_message(f"{PREFIX} §bChecking Discord ID…")

        def job() -> None:
            if self.settings.require_guild_membership:
                self.discord.get_member(discord_id)

        def done(_result, error: Exception | None) -> None:
            online = self._online_player(player_uuid, player_name)
            if error is not None:
                self.storage.cancel_pending(player_uuid)
                if online is not None:
                    if isinstance(error, DiscordApiError) and error.status == 404:
                        online.send_message(f"{PREFIX} §cThat Discord account is not in the configured Discord server.")
                    else:
                        online.send_message(f"{PREFIX} §cDiscord could not validate that account. Please try again or contact staff.")
                self.logger.warning(f"Discord account validation failed for {player_name}: {error}")
                return
            if online is None:
                return
            self.refresh_profile(online)
            self.forms.show_code(online, code)

        self._submit(job, done)

    def send_link_status(self, player: Player) -> None:
        link = self.storage.get_link(str(player.unique_id))
        if link is None:
            player.send_message(f"{PREFIX} §eNot linked. §7Use §f/link§7 to start.")
        else:
            player.send_message(f"{PREFIX} §aLinked. §7Discord roles sync automatically.")

    def unlink_player(self, player: Player) -> None:
        player_uuid = str(player.unique_id)
        previous_roles = self.storage.get_managed_roles(player_uuid)
        link = self.storage.unlink(player_uuid)
        if link is None:
            player.send_message(f"{PREFIX} §eYour account is not linked.")
            return
        self.queue_remove_roles(link, previous_roles)
        player.send_message(f"{PREFIX} §aUnlinked. §7Managed Discord roles are being removed.")

    def admin_unlink(self, sender: CommandSender, identifier: str) -> None:
        found = self.storage.find_link(identifier)
        if found is None:
            sender.send_message(f"{PREFIX} §cNo linked account matched §f{identifier}§c.")
            return
        previous_roles = self.storage.get_managed_roles(found.player_uuid)
        link = self.storage.unlink(found.player_uuid)
        if link is None:
            sender.send_message(f"{PREFIX} §cNo linked account matched §f{identifier}§c.")
            return
        self.queue_remove_roles(link, previous_roles)
        sender.send_message(f"{PREFIX} §aUnlinked §f{link.player_name} §7from Discord §f{link.discord_id}§7.")

    def finalize_discord_verification(
        self,
        link: LinkRecord,
        previous_link: LinkRecord | None,
        discord_username: str,
        application_id: str,
        interaction_token: str,
    ) -> None:
        previous_roles = self.storage.get_managed_roles(link.player_uuid)
        if previous_link is not None and previous_link.discord_id != link.discord_id:
            self.queue_remove_roles(previous_link, previous_roles, clear_state=False)

        def finish(sync_result, sync_error: Exception | None, player_online: bool) -> None:
            payload = self.discord_ui.success_payload(link, discord_username, sync_result, sync_error, player_online)

            def edit_job():
                if not application_id or not interaction_token:
                    return {}
                return self.discord.edit_original_interaction(application_id, interaction_token, payload)

            def edit_done(_result, error: Exception | None) -> None:
                if error is not None:
                    self.logger.warning(f"Could not update Discord verification result for {link.player_name}: {error}")

            self._submit(edit_job, edit_done)

        def task() -> None:
            player = self._online_player(link.player_uuid, link.player_name)
            if player is not None:
                player.send_message(f"{PREFIX} §aLinked successfully. §7Discord roles are syncing automatically.")
                self._sync_player(player, notify=False, callback=lambda result, error: finish(result, error, True))
                return
            self._sync_verified_role_only(link, lambda result, error: finish(result, error, False))

        try:
            self.server.scheduler.run_task(self, task)
        except Exception as exc:
            self.logger.warning(f"Could not schedule post-verification sync for {link.player_name}: {exc}")
            finish(None, exc, False)

    def refresh_profile(self, player: Player) -> ProfileContext:
        snapshot = self.integrations.snapshot(player)
        groups = set()
        if self.settings.enable_stoneperms:
            groups |= snapshot.stoneperms_groups
            if snapshot.stoneperms_primary:
                groups.add(snapshot.stoneperms_primary)
        if self.settings.enable_pureperms:
            groups |= snapshot.pureperms_groups
            if snapshot.pureperms_primary:
                groups.add(snapshot.pureperms_primary)
        primary = ""
        if self.settings.enable_stoneperms and snapshot.stoneperms_primary:
            primary = snapshot.stoneperms_primary
        elif self.settings.enable_pureperms and snapshot.pureperms_primary:
            primary = snapshot.pureperms_primary
        elif groups:
            primary = sorted(groups, key=str.casefold)[0]
        profile = ProfileContext(
            primary_group=primary,
            groups=tuple(sorted((str(x) for x in groups if str(x).strip()), key=str.casefold)),
            faction=snapshot.faction_name or "",
            faction_rank=snapshot.faction_rank or "",
            balance=snapshot.balance,
        )
        self._profile_cache[str(player.unique_id)] = profile
        return profile

    def placeholder_context(self, link: LinkRecord, extra: dict[str, str] | None = None) -> dict[str, str]:
        profile = self._profile_cache.get(link.player_uuid, ProfileContext("", (), "", "", None))
        balance = "" if profile.balance is None else f"{profile.balance:,.2f}"
        values = {
            "player": link.player_name,
            "player_url": quote(link.player_name, safe=""),
            "uuid": link.player_uuid,
            "xuid": link.xuid or "",
            "xuid_url": quote(link.xuid or "", safe=""),
            "discord_id": link.discord_id,
            "discord_mention": f"<@{link.discord_id}>",
            "discord_username": "",
            "verified_at": str(link.verified_at),
            "verified_timestamp": f"<t:{link.verified_at}:F>",
            "group": profile.primary_group or "",
            "groups": ", ".join(profile.groups) if profile.groups else "",
            "faction": profile.faction or "",
            "faction_rank": profile.faction_rank or "",
            "balance": balance,
            "role_sync_status": "",
            "roles_added": "0",
            "roles_removed": "0",
            "role_changes": "0",
            "server_name": self.settings.server_name,
            "server_link": self.settings.server_invite_url,
            "guild_id": self.settings.guild_id,
            "verification_channel_id": self.settings.verification_channel_id,
        }
        if extra:
            values.update({str(k): str(v) for k, v in extra.items()})
        return values

    def sync_player(self, player: Player, *, notify: bool) -> bool:
        return self._sync_player(player, notify=notify, callback=None)

    def _sync_player(self, player: Player, *, notify: bool, callback) -> bool:
        link = self.storage.get_link(str(player.unique_id))
        if link is None or not self.discord.configured:
            if notify:
                player.send_message(f"{PREFIX} §eLink your Discord account first.")
            if callback is not None:
                callback(None, RuntimeError("player is not linked or Discord is not configured"))
            return False
        snapshot = self.integrations.snapshot(player)
        self._cache_snapshot(player, snapshot)
        desired, managed = self._roles_for(snapshot)
        player_uuid = str(player.unique_id)
        previous_managed = self.storage.get_managed_roles(player_uuid)
        managed |= previous_managed
        player_name = player.name
        if notify:
            player.send_message(f"{PREFIX} §bRefreshing Discord roles…")

        def job():
            return self.discord.sync_roles(
                link.discord_id,
                desired=desired,
                managed=managed,
                remove_managed=self.settings.remove_managed_roles,
            )

        def done(result, error: Exception | None) -> None:
            online = self._online_player(player_uuid, player_name)
            if error is not None:
                self.logger.warning(f"Role sync failed for {player_name}: {error}")
                if notify and online is not None:
                    online.send_message(f"{PREFIX} §cRole sync failed. §7Ask staff to check the bot role hierarchy and permissions.")
                if callback is not None:
                    callback(result, error)
                return
            if self.settings.remove_managed_roles:
                self.storage.set_managed_roles(player_uuid, set(desired))
            else:
                self.storage.set_managed_roles(player_uuid, previous_managed | set(desired))
            if notify and online is not None:
                changed = len(result.added) + len(result.removed)
                online.send_message(f"{PREFIX} §aRoles refreshed. §7{changed} change(s).")
            if callback is not None:
                callback(result, None)

        self._submit(job, done)
        return True

    def _sync_verified_role_only(self, link: LinkRecord, callback) -> None:
        verified = self.settings.verified_role_id.strip()
        desired = {verified} if verified else set()
        previous_managed = self.storage.get_managed_roles(link.player_uuid)
        managed = ({verified} if verified else set()) | previous_managed

        def job():
            return self.discord.sync_roles(
                link.discord_id,
                desired=desired,
                managed=managed,
                remove_managed=self.settings.remove_managed_roles,
            )

        def done(result, error: Exception | None) -> None:
            if error is None:
                if self.settings.remove_managed_roles:
                    self.storage.set_managed_roles(link.player_uuid, set(desired))
                else:
                    self.storage.set_managed_roles(link.player_uuid, previous_managed | set(desired))
            if callback is not None:
                callback(result, error)

        self._submit(job, done)

    def sync_all_online(self, notify_sender: CommandSender | None = None) -> None:
        queued = 0
        for player in tuple(self.server.online_players):
            if self.sync_player(player, notify=False):
                queued += 1
        if notify_sender is not None:
            notify_sender.send_message(f"{PREFIX} §aQueued role sync for §f{queued} §aonline linked player(s).")

    def queue_remove_roles(self, link: LinkRecord, extra_roles: set[str] | None = None, clear_state: bool = True) -> None:
        if not self.discord.configured:
            if clear_state:
                self.storage.clear_managed_roles(link.player_uuid)
            return
        managed = self._managed_roles() | set(extra_roles or ())

        def done(_result, error: Exception | None) -> None:
            if error is not None:
                self.logger.warning(f"Could not remove managed roles for {link.player_name}: {error}")
                return
            if clear_state:
                self.storage.clear_managed_roles(link.player_uuid)

        self._submit(
            lambda: self.discord.sync_roles(link.discord_id, desired=set(), managed=managed, remove_managed=True),
            done,
        )

    def queue_discord_panel(self, channel_id: str) -> None:
        def done(_result, error: Exception | None) -> None:
            if error is not None:
                self.logger.warning(f"Could not send Discord verification panel to {channel_id}: {error}")

        self._submit(lambda: self.discord_ui.send_panel(channel_id), done)

    def send_panel_from_minecraft(self, sender: CommandSender, channel_id: str | None) -> None:
        target = (channel_id or self.settings.verification_channel_id).strip()
        if not target:
            sender.send_message(f"{PREFIX} §cSet discord.verification_channel_id or provide a channel ID.")
            return
        if not self.discord.configured:
            sender.send_message(f"{PREFIX} §cDiscord bot is not configured.")
            return
        sender.send_message(f"{PREFIX} §7Sending the Components V2 verification panel…")

        def done(result, error: Exception | None) -> None:
            if error is not None:
                sender.send_message(f"{PREFIX} §cCould not send the Discord panel: {error}")
                return
            message_id = result.get("id", "") if isinstance(result, dict) else ""
            sender.send_message(f"{PREFIX} §aVerification panel sent to Discord channel §f{target}§a. §7Message ID: §f{message_id or 'unknown'}")

        self._submit(lambda: self.discord_ui.send_panel(target), done)

    def reload_runtime(self, sender: CommandSender | None = None) -> None:
        try:
            self.reload_config()
            self._load_settings()
            self._restart_discord_runtime()
            if sender is not None:
                sender.send_message(f"{PREFIX} §aConfiguration reloaded.")
            self.log_integrations()
        except Exception as exc:
            self.logger.error(f"Failed to reload config: {exc}")
            if sender is not None:
                sender.send_message(f"{PREFIX} §cConfig reload failed: {exc}")

    def log_integrations(self) -> None:
        status = self.integrations.detection_status()
        summary = ", ".join(f"{name}={'yes' if value else 'no'}" for name, value in status.items())
        self.logger.info(f"Optional integrations: {summary}")

    def ui_value(self, section: str, key: str, default):
        ui = _table(self.config, "ui")
        table = ui.get(section, {})
        if not isinstance(table, dict):
            return default
        return table.get(key, default)

    def _roles_for(self, snap: IntegrationSnapshot) -> tuple[set[str], set[str]]:
        cfg = _table(self.config, "role_sync")
        desired: set[str] = set()
        managed: set[str] = set()
        verified = self.settings.verified_role_id.strip()
        if verified:
            desired.add(verified)
            managed.add(verified)
        groups = set()
        if self.settings.enable_stoneperms:
            groups |= snap.stoneperms_groups
            if snap.stoneperms_primary:
                groups.add(snap.stoneperms_primary)
        if self.settings.enable_pureperms:
            groups |= snap.pureperms_groups
            if snap.pureperms_primary:
                groups.add(snap.pureperms_primary)
        self._apply_map(cfg, "groups", groups, desired, managed)
        if self.settings.enable_factions:
            self._apply_map(cfg, "faction_ranks", {snap.faction_rank} if snap.faction_rank else set(), desired, managed)
            self._apply_map(cfg, "factions", {snap.faction_name} if snap.faction_name else set(), desired, managed)
        if self.settings.enable_economy:
            tiers = cfg.get("economy_tiers", [])
            if isinstance(tiers, list):
                for tier in tiers:
                    if not isinstance(tier, dict):
                        continue
                    role_id = str(tier.get("role_id", "")).strip()
                    if not role_id:
                        continue
                    managed.add(role_id)
                    if snap.balance is None:
                        continue
                    minimum = float(tier.get("min_balance", 0))
                    raw_max = tier.get("max_balance")
                    maximum = float(raw_max) if raw_max not in (None, "", -1) else None
                    if snap.balance >= minimum and (maximum is None or snap.balance <= maximum):
                        desired.add(role_id)
        return desired, managed

    @staticmethod
    def _apply_map(cfg: dict, section: str, values: set[str], desired: set[str], managed: set[str]) -> None:
        table = cfg.get(section, {})
        if not isinstance(table, dict):
            return
        folded = {str(value).casefold() for value in values if str(value).strip()}
        for key, role in table.items():
            role_id = str(role).strip()
            if not role_id:
                continue
            managed.add(role_id)
            if str(key).casefold() in folded:
                desired.add(role_id)

    def _managed_roles(self) -> set[str]:
        cfg = _table(self.config, "role_sync")
        managed = set()
        verified = self.settings.verified_role_id.strip()
        if verified:
            managed.add(verified)
        for section in ("groups", "faction_ranks", "factions"):
            table = cfg.get(section, {})
            if isinstance(table, dict):
                managed.update(str(value).strip() for value in table.values() if str(value).strip())
        tiers = cfg.get("economy_tiers", [])
        if isinstance(tiers, list):
            managed.update(str(tier.get("role_id", "")).strip() for tier in tiers if isinstance(tier, dict) and str(tier.get("role_id", "")).strip())
        return managed

    def _cache_snapshot(self, player: Player, snapshot: IntegrationSnapshot) -> None:
        groups = set()
        if self.settings.enable_stoneperms:
            groups |= snapshot.stoneperms_groups
            if snapshot.stoneperms_primary:
                groups.add(snapshot.stoneperms_primary)
        if self.settings.enable_pureperms:
            groups |= snapshot.pureperms_groups
            if snapshot.pureperms_primary:
                groups.add(snapshot.pureperms_primary)
        primary = ""
        if self.settings.enable_stoneperms and snapshot.stoneperms_primary:
            primary = snapshot.stoneperms_primary
        elif self.settings.enable_pureperms and snapshot.pureperms_primary:
            primary = snapshot.pureperms_primary
        elif groups:
            primary = sorted(groups, key=str.casefold)[0]
        self._profile_cache[str(player.unique_id)] = ProfileContext(
            primary_group=primary,
            groups=tuple(sorted((str(x) for x in groups if str(x).strip()), key=str.casefold)),
            faction=snapshot.faction_name or "",
            faction_rank=snapshot.faction_rank or "",
            balance=snapshot.balance,
        )

    def _periodic_tick(self) -> None:
        if not self._enabled_runtime:
            return
        now = time.monotonic()
        if now - self._last_periodic_sync < self.settings.sync_interval_seconds:
            return
        self._last_periodic_sync = now
        self.sync_all_online()

    def _send_admin_status(self, sender: CommandSender) -> None:
        sender.send_message(f"{PREFIX} §7Linked accounts: §f{self.storage.count_links()}")
        sender.send_message(f"{PREFIX} §7Pending codes: §f{self.storage.count_pending()}")
        sender.send_message(f"{PREFIX} §7Discord bot: {'§aconfigured' if self.discord.configured else '§cnot configured'}")
        sender.send_message(f"{PREFIX} §7Discord gateway: {'§arunning' if self.gateway_running else '§cstopped'}")
        for name, ok in self.integrations.detection_status().items():
            sender.send_message(f"{PREFIX} §7{name}: {'§aDetected' if ok else '§8Not detected'}")

    def _load_settings(self) -> None:
        root = self.config
        server = _table(root, "server")
        discord = _table(root, "discord")
        security = _table(root, "security")
        integrations = _table(root, "integrations")
        role_sync = _table(root, "role_sync")
        self.settings = Settings(
            token=str(discord.get("bot_token", "")),
            guild_id=str(discord.get("guild_id", "")),
            verification_channel_id=str(discord.get("verification_channel_id", "")),
            server_name=str(server.get("name", "Minecraft Server")),
            server_invite_url=str(server.get("discord_invite", "")),
            require_guild_membership=bool(discord.get("require_guild_membership", True)),
            restrict_to_verification_channel=bool(discord.get("restrict_to_verification_channel", True)),
            gateway_enabled=bool(discord.get("gateway_enabled", True)),
            register_commands=bool(discord.get("register_commands", True)),
            verified_role_id=str(role_sync.get("verified_role_id", "")),
            remove_managed_roles=bool(role_sync.get("remove_managed_roles", True)),
            sync_interval_seconds=max(10, int(role_sync.get("sync_interval_seconds", 60))),
            code_length=max(6, min(10, int(security.get("code_length", 6)))),
            expiry_seconds=max(60, min(3600, int(security.get("expiry_seconds", 600)))),
            max_attempts=max(1, min(20, int(security.get("max_attempts", 5)))),
            cooldown_seconds=max(0, min(600, int(security.get("cooldown_seconds", 60)))),
            allow_relink=bool(security.get("allow_relink", False)),
            enable_stoneperms=bool(integrations.get("stoneperms", True)),
            enable_pureperms=bool(integrations.get("pureperms", True)),
            enable_factions=bool(integrations.get("factions", True)),
            enable_economy=bool(integrations.get("bedrockeconomy", True)),
        )
        self.discord = DiscordClient(self.settings.token, self.settings.guild_id)
        self.discord_ui = DiscordInteractionController(self)

    def _restart_discord_runtime(self) -> None:
        self._runtime_generation += 1
        generation = self._runtime_generation
        if self._gateway is not None:
            self._gateway.stop()
            self._gateway = None
        if not self.discord.configured:
            self.logger.warning("Discord bot is not configured yet. Set discord.bot_token and discord.guild_id in config.toml.")
            return

        client = self.discord
        controller = self.discord_ui
        settings = self.settings

        def job():
            user = client.test()
            application = client.get_application()
            application_id = str(application.get("id", ""))
            if settings.register_commands and application_id:
                controller.register_commands(application_id)
            return user, application_id

        def done(result, error: Exception | None) -> None:
            if generation != self._runtime_generation:
                return
            if error is not None:
                self.logger.error(f"Discord bot setup failed: {error}")
                return
            user, application_id = result
            username = user.get("username", "bot") if isinstance(user, dict) else "bot"
            self.logger.info(f"Discord bot authenticated as {username}; application id={application_id or 'unknown'}")
            if settings.gateway_enabled:
                self._gateway = DiscordGateway(client, controller.handle, self.logger)
                self._gateway.start()

        self._submit(job, done)

    def _submit(self, job, callback) -> None:
        if not self._enabled_runtime:
            return
        future: Future = self._executor.submit(job)

        def complete(done: Future) -> None:
            try:
                result, error = done.result(), None
            except Exception as exc:
                result, error = None, exc
            if not self._enabled_runtime:
                return
            try:
                self.server.scheduler.run_task(self, lambda: callback(result, error))
            except Exception:
                pass

        future.add_done_callback(complete)

    def _online_player(self, player_uuid: str, player_name: str) -> Player | None:
        try:
            for player in tuple(self.server.online_players):
                if str(player.unique_id) == player_uuid:
                    return player
        except Exception:
            pass
        try:
            candidate = self.server.get_player(player_name)
            if candidate is not None and str(candidate.unique_id) == player_uuid:
                return candidate
        except Exception:
            pass
        return None


def _table(root: dict, key: str) -> dict:
    value = root.get(key, {})
    return value if isinstance(value, dict) else {}


def _xuid(player: Player) -> str | None:
    try:
        value = str(player.xuid).strip() if player.xuid else ""
    except Exception:
        value = ""
    return value or None


def _valid_discord_id(value: str) -> bool:
    return value.isdigit() and 17 <= len(value) <= 20


def _code(length: int) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(length))
