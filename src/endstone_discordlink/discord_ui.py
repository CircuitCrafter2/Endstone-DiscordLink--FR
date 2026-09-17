from __future__ import annotations

import re
from urllib.parse import quote


class DiscordInteractionController:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def register_commands(self, application_id: str) -> None:
        commands = [
            {
                "name": "verify",
                "type": 1,
                "description": self.plugin.ui_value("commands", "verify_description", "Verify and link your Minecraft account"),
            },
            {
                "name": "unlink",
                "type": 1,
                "description": self.plugin.ui_value("commands", "unlink_description", "Unlink your Minecraft account"),
            },
            {
                "name": "linkpanel",
                "type": 1,
                "description": self.plugin.ui_value("commands", "panel_description", "Send the DiscordLink verification panel"),
                "default_member_permissions": "32",
            },
        ]
        for command in commands:
            self.plugin.discord.upsert_guild_command(application_id, command)

    def handle(self, interaction: dict) -> None:
        interaction_type = int(interaction.get("type", 0))
        data = interaction.get("data", {}) or {}
        if interaction_type == 2:
            self._command(interaction, str(data.get("name", "")).casefold())
            return
        if interaction_type == 3:
            self._component(interaction, str(data.get("custom_id", "")))
            return
        if interaction_type == 5:
            self._modal(interaction, str(data.get("custom_id", "")))

    def send_panel(self, channel_id: str) -> dict:
        return self.plugin.discord.send_components_message(channel_id, self.panel_components())

    def panel_components(self) -> list[dict]:
        title = self.plugin.ui_value("panel", "title", "## 🔗 Minecraft Account Link")
        description = self.plugin.ui_value("panel", "description", "Link your Minecraft account to Discord in under a minute.")
        instructions = self.plugin.ui_value(
            "panel",
            "instructions",
            "**1.** Run `/link` in Minecraft\n**2.** Enter your Discord ID\n**3.** Press **Verify** and enter the 6-digit code",
        )
        footer = self.plugin.ui_value("panel", "footer", "-# Your code is temporary and locked to your Discord account.")
        if str(description).strip() == "Connect your Minecraft and Discord accounts securely using a temporary code generated in-game.":
            description = "Link your Minecraft account to Discord in under a minute."
        instructions = re.sub(r"\s*Once linked,\s*your configured Minecraft ranks,\s*faction roles and economy roles can sync automatically\.\s*", "", str(instructions), flags=re.IGNORECASE).strip()
        thumbnail = self.plugin.ui_value("panel", "thumbnail_url", "")
        inner = [{"type": 10, "content": title}]
        if thumbnail:
            inner.append(
                {
                    "type": 9,
                    "components": [{"type": 10, "content": description}],
                    "accessory": {"type": 11, "media": {"url": thumbnail}, "description": "Discord verification"},
                }
            )
        else:
            inner.append({"type": 10, "content": description})
        inner.extend(
            [
                {"type": 14, "divider": True, "spacing": 1},
                {"type": 10, "content": instructions},
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "style": 1,
                            "custom_id": "discordlink:verify",
                            "label": self.plugin.ui_value("panel", "verify_button", "Verify"),
                            "emoji": {"name": "✅"},
                        },
                        {
                            "type": 2,
                            "style": 4,
                            "custom_id": "discordlink:unlink",
                            "label": self.plugin.ui_value("panel", "unlink_button", "Unlink"),
                            "emoji": {"name": "🔓"},
                        },
                    ],
                },
                {"type": 10, "content": footer},
            ]
        )
        return [{"type": 17, "accent_color": self._color("panel", "accent_color", 0x5865F2), "components": inner}]

    def success_payload(self, link, discord_username: str, sync_result, sync_error: Exception | None, player_online: bool) -> dict:
        added = len(getattr(sync_result, "added", ()) or ()) if sync_result is not None else 0
        removed = len(getattr(sync_result, "removed", ()) or ()) if sync_result is not None else 0
        if sync_error is not None:
            sync_status = "⚠️ Linked • role sync needs staff attention"
        elif not player_online:
            sync_status = "⏳ Linked • full role sync runs on your next Minecraft join"
        elif added or removed:
            sync_status = f"✅ Synced automatically • +{added} / -{removed} role changes"
        else:
            sync_status = "✅ Synced automatically • roles already up to date"
        context = self.plugin.placeholder_context(
            link,
            {
                "discord_username": discord_username,
                "role_sync_status": sync_status,
                "roles_added": str(added),
                "roles_removed": str(removed),
                "role_changes": str(added + removed),
            },
        )
        body = self._success_body(context)
        show_character = bool(self.plugin.ui_value("success", "show_player_character", True))
        thumbnail_template = self.plugin.ui_value("success", "thumbnail_url", "https://api.mcheads.org/head/.{player_url}/128")
        thumbnail = _format(thumbnail_template, context) if show_character else ""
        components = self._card_components(body, "success" if sync_error is None else "neutral", thumbnail, include_unlink=True)
        return {"components": components}

    def _success_body(self, context: dict[str, str]) -> str:
        title = _format(self.plugin.ui_value("success", "title", "## ✅ Account linked"), context)
        default_fields = [
            "Minecraft|`{player}`",
            "Discord|{discord_mention}",
            "Group|`{group}`",
            "Faction|`{faction}`",
            "Faction Rank|`{faction_rank}`",
            "Balance|`{balance}`",
            "XUID|`{xuid}`",
            "Verified|{verified_timestamp}",
        ]
        fields = self.plugin.ui_value("success", "fields", default_fields)
        hide_empty = bool(self.plugin.ui_value("success", "hide_empty_fields", True))
        lines = [title]
        if isinstance(fields, list) and fields:
            for raw in fields:
                text = str(raw)
                if "|" in text:
                    label, value_template = text.split("|", 1)
                    if hide_empty and not _template_has_value(value_template, context):
                        continue
                    value = _format(value_template, context).strip()
                    if hide_empty and not value:
                        continue
                    lines.append(f"**{_format(label, context).strip()}:** {value}")
                else:
                    if hide_empty and not _template_has_value(text, context):
                        continue
                    value = _format(text, context).strip()
                    if value:
                        lines.append(value)
        else:
            body = self.plugin.ui_value(
                "success",
                "body",
                "## ✅ Account linked\n**Minecraft:** `{player}`\n**Discord:** {discord_mention}\n**Group:** `{group}`\n**Faction:** `{faction}`\n**Balance:** `{balance}`\n\n{role_sync_status}",
            )
            return _format(body, context)
        status = _format(self.plugin.ui_value("success", "sync_line", "{role_sync_status}"), context).strip()
        if status:
            lines.extend(["", status])
        footer = _format(self.plugin.ui_value("success", "footer", "-# Role syncing is automatic while you play."), context).strip()
        if footer:
            lines.append(footer)
        return "\n".join(lines)

    def _command(self, interaction: dict, name: str) -> None:
        if name == "verify":
            if self._require_channel(interaction):
                self._show_verify_modal(interaction)
            return
        if name == "unlink":
            if self._require_channel(interaction):
                self._show_unlink_confirm(interaction)
            return
        if name == "linkpanel":
            if not self._is_admin(interaction):
                self._ephemeral(interaction, "## ❌ No permission\nYou need **Manage Server** to send the verification panel.", "error")
                return
            channel_id = self.plugin.settings.verification_channel_id or str(interaction.get("channel_id", ""))
            if not channel_id:
                self._ephemeral(interaction, "## ❌ Channel unavailable\nI could not determine where to send the panel.", "error")
                return
            self._ephemeral(interaction, f"## ✅ Panel queued\nPosting the verification panel in <#{channel_id}>.", "success")
            self.plugin.queue_discord_panel(channel_id)

    def _component(self, interaction: dict, custom_id: str) -> None:
        if custom_id in {"discordlink:verify", "discordauth:verify"}:
            if self._require_channel(interaction):
                self._show_verify_modal(interaction)
            return
        if custom_id in {"discordlink:unlink", "discordauth:unlink"}:
            if self._require_channel(interaction):
                self._show_unlink_confirm(interaction)
            return
        if custom_id in {"discordlink:unlink_confirm", "discordauth:unlink_confirm"}:
            self._unlink(interaction)
            return
        if custom_id in {"discordlink:unlink_cancel", "discordauth:unlink_cancel"}:
            self._ephemeral(interaction, "## Cancelled\nYour Minecraft account is still linked.", "neutral")

    def _modal(self, interaction: dict, custom_id: str) -> None:
        if custom_id not in {"discordlink:verify_modal", "discordauth:verify_modal"}:
            return
        discord_id = _user_id(interaction)
        code = _find_component_value(interaction.get("data", {}).get("components", []), "discordlink:code") or _find_component_value(interaction.get("data", {}).get("components", []), "discordauth:code")
        if not discord_id:
            self._ephemeral(interaction, "## ❌ Verification failed\nI could not identify your Discord account.", "error")
            return
        result = self.plugin.storage.verify_pending_by_discord(discord_id, code.strip(), self.plugin.settings.max_attempts)
        if not result.ok or result.link is None:
            messages = {
                "missing": "No active code exists for your Discord account. Run `/link` in Minecraft first.",
                "expired": "That code expired. Run `/link` in Minecraft for a new one.",
                "invalid": "That code is incorrect. Check the code shown in Minecraft.",
                "attempts": "Too many incorrect attempts. Run `/link` in Minecraft for a new code.",
                "discord-in-use": "This Discord account is already linked to another Minecraft account.",
            }
            self._ephemeral(interaction, f"## ❌ Verification failed\n{messages.get(result.reason, 'The verification request could not be completed.')}", "error")
            return
        user = ((interaction.get("member", {}) or {}).get("user", {}) or interaction.get("user", {}) or {})
        discord_username = str(user.get("global_name") or user.get("username") or "")
        application_id = str(interaction.get("application_id", ""))
        interaction_token = str(interaction.get("token", ""))
        linking_body = self.plugin.ui_value("success", "linking_body", "## ✅ Code accepted\nLinking your account and syncing Discord roles…")
        components = self._card_components(str(linking_body), "success", "", include_unlink=False)
        self._response(interaction, {"type": 4, "data": {"flags": 32832, "components": components}})
        self.plugin.finalize_discord_verification(
            result.link,
            result.previous_link,
            discord_username,
            application_id,
            interaction_token,
        )

    def _show_verify_modal(self, interaction: dict) -> None:
        length = self.plugin.settings.code_length
        payload = {
            "type": 9,
            "data": {
                "custom_id": "discordlink:verify_modal",
                "title": self.plugin.ui_value("verify_modal", "title", "Verify Minecraft Account")[:45],
                "components": [
                    {
                        "type": 18,
                        "label": self.plugin.ui_value("verify_modal", "label", "Verification code")[:45],
                        "description": self.plugin.ui_value("verify_modal", "description", "Enter the code shown in Minecraft after /link.")[:100],
                        "component": {
                            "type": 4,
                            "custom_id": "discordlink:code",
                            "style": 1,
                            "min_length": length,
                            "max_length": length,
                            "placeholder": "0" * length,
                            "required": True,
                        },
                    }
                ],
            },
        }
        self._response(interaction, payload)

    def _show_unlink_confirm(self, interaction: dict) -> None:
        discord_id = _user_id(interaction)
        link = self.plugin.storage.get_link_by_discord(discord_id) if discord_id else None
        if link is None:
            self._ephemeral(interaction, "## ℹ️ Nothing to unlink\nYour Discord account is not linked to a Minecraft account.", "neutral")
            return
        context = self.plugin.placeholder_context(link)
        body = self.plugin.ui_value("unlink", "confirm_body", "## 🔓 Unlink account?\nUnlink **{player}** from {discord_mention}?")
        components = self._card_components(_format(body, context), "unlink", "", include_unlink=False)
        components[0]["components"].append(
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 4, "custom_id": "discordlink:unlink_confirm", "label": self.plugin.ui_value("unlink", "confirm_button", "Unlink")},
                    {"type": 2, "style": 2, "custom_id": "discordlink:unlink_cancel", "label": self.plugin.ui_value("unlink", "cancel_button", "Cancel")},
                ],
            }
        )
        self._response(interaction, {"type": 4, "data": {"flags": 32832, "components": components}})

    def _unlink(self, interaction: dict) -> None:
        discord_id = _user_id(interaction)
        link = self.plugin.storage.get_link_by_discord(discord_id) if discord_id else None
        if link is None:
            self._ephemeral(interaction, "## ℹ️ Nothing to unlink\nYour Discord account is not linked.", "neutral")
            return
        previous_roles = self.plugin.storage.get_managed_roles(link.player_uuid)
        removed = self.plugin.storage.unlink(link.player_uuid)
        if removed is None:
            self._ephemeral(interaction, "## ❌ Unlink failed\nThe account link could not be removed.", "error")
            return
        self.plugin.queue_remove_roles(removed, previous_roles)
        context = self.plugin.placeholder_context(removed)
        body = self.plugin.ui_value("unlink", "success_body", "## ✅ Account unlinked\n**{player}** is no longer linked. Managed roles are being removed.")
        self._ephemeral(interaction, _format(body, context), "success")

    def _require_channel(self, interaction: dict) -> bool:
        required = self.plugin.settings.verification_channel_id
        if not required or not self.plugin.settings.restrict_to_verification_channel:
            return True
        channel_id = str(interaction.get("channel_id", ""))
        if channel_id == required:
            return True
        self._ephemeral(interaction, f"## ℹ️ Use the verification channel\nPlease verify in <#{required}>.", "neutral")
        return False

    def _is_admin(self, interaction: dict) -> bool:
        permissions = str((interaction.get("member", {}) or {}).get("permissions", "0"))
        try:
            value = int(permissions)
        except ValueError:
            return False
        return bool(value & 0x8 or value & 0x20)

    def _ephemeral(self, interaction: dict, body: str, style: str) -> None:
        components = self._card_components(body, style, "", include_unlink=False)
        self._response(interaction, {"type": 4, "data": {"flags": 32832, "components": components}})

    def _card_components(self, body: str, style: str, thumbnail: str, include_unlink: bool) -> list[dict]:
        inner = []
        if thumbnail:
            inner.append(
                {
                    "type": 9,
                    "components": [{"type": 10, "content": body}],
                    "accessory": {"type": 11, "media": {"url": thumbnail}, "description": "Minecraft character"},
                }
            )
        else:
            inner.append({"type": 10, "content": body})
        if include_unlink:
            inner.append({"type": 14, "divider": True, "spacing": 1})
            inner.append(
                {
                    "type": 1,
                    "components": [
                        {"type": 2, "style": 4, "custom_id": "discordlink:unlink", "label": self.plugin.ui_value("success", "unlink_button", "Unlink")}
                    ],
                }
            )
        return [{"type": 17, "accent_color": self._color(style, "accent_color", self._fallback_color(style)), "components": inner}]

    def _response(self, interaction: dict, payload: dict) -> None:
        interaction_id = str(interaction.get("id", ""))
        token = str(interaction.get("token", ""))
        if interaction_id and token:
            self.plugin.discord.interaction_callback(interaction_id, token, payload)

    def _color(self, section: str, key: str, default: int) -> int:
        value = self.plugin.ui_value(section, "embed_color", None)
        if value is None:
            value = self.plugin.ui_value(section, key, default)
        if isinstance(value, int):
            return max(0, min(value, 0xFFFFFF))
        text = str(value).strip().lstrip("#")
        try:
            return int(text, 16) & 0xFFFFFF
        except ValueError:
            return default

    @staticmethod
    def _fallback_color(style: str) -> int:
        return {"success": 0x57F287, "error": 0xED4245, "unlink": 0xFEE75C, "neutral": 0x5865F2}.get(style, 0x5865F2)


def _user_id(interaction: dict) -> str:
    member = interaction.get("member", {}) or {}
    user = member.get("user", {}) or interaction.get("user", {}) or {}
    return str(user.get("id", ""))


def _find_component_value(components: list, custom_id: str) -> str:
    for item in components:
        if not isinstance(item, dict):
            continue
        if str(item.get("custom_id", "")) == custom_id and "value" in item:
            return str(item.get("value", ""))
        child = item.get("component")
        if isinstance(child, dict):
            found = _find_component_value([child], custom_id)
            if found:
                return found
        children = item.get("components")
        if isinstance(children, list):
            found = _find_component_value(children, custom_id)
            if found:
                return found
    return ""


def _format(template: str, context: dict[str, str]) -> str:
    values = _SafeValues(context)
    return str(template).format_map(values)


def _template_has_value(template: str, context: dict[str, str]) -> bool:
    keys = re.findall(r"\{([A-Za-z0-9_]+)\}", str(template))
    if not keys:
        return bool(str(template).strip())
    return any(str(context.get(key, "")).strip() for key in keys)


class _SafeValues(dict):
    def __missing__(self, key: str) -> str:
        return ""


def url_value(value: str) -> str:
    return quote(value, safe="")
