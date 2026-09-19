from __future__ import annotations

import json

from endstone.form import ActionForm, MessageForm, ModalForm, TextInput

class DiscordForms:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def open_player(self, player) -> None:
        link = self.plugin.storage.get_link(str(player.unique_id))
        if link is None:
            context = {
                "server_name": self.plugin.settings.server_name,
                "discord_invite": self.plugin.settings.server_invite_url,
                "verification_channel_id": self.plugin.settings.verification_channel_id,
            }
            content = self._text(
                "minecraft_link",
                "content",
                "§fLie ton compte Minecraft à Discord.\n\n§71. Appuie sur §aLier Discord\n§72. Entre ton ID Discord\n§73. Entre le code à 6 chiffres dans Discord\n\n§b{discord_invite}",
                context,
            )
            if not self.plugin.settings.server_invite_url:
                content = content.replace("\n\n§b", "")
            form = ActionForm(title=self._text("minecraft_link", "title", "§l§9Lien Discord", context), content=content)
            form.add_button(
                self._text("minecraft_link", "link_button", "§a§lLIER DISCORD\n§r§7Générer un code", context),
                on_click=self._open_id,
            )
            form.add_button(self._text("minecraft_link", "close_button", "§8Fermer", context))
            player.send_form(form)
            return
        context = {
            "player": link.player_name,
            "discord_id": link.discord_id,
            "server_name": self.plugin.settings.server_name,
        }
        form = ActionForm(
            title=self._text("minecraft_linked", "title", "§l§9Lien Discord", context),
            content=self._text(
                "minecraft_linked",
                "content",
                "§a§l✓ LIÉ§r\n§7Tes rôles Discord se synchronisent automatiquement.",
                context,
            ),
        )
        form.add_button(
            self._text("minecraft_linked", "sync_button", "§eRafraîchir les rôles\n§7Uniquement nécessaire si quelque chose semble incorrect", context),
            on_click=lambda p: self.plugin.sync_player(p, notify=True),
        )
        form.add_button(
            self._text("minecraft_linked", "unlink_button", "§cDélier Discord\n§7Supprimer ce lien de compte", context),
            on_click=self._confirm_unlink,
        )
        form.add_button(self._text("minecraft_linked", "close_button", "§8Fermer", context))
        player.send_form(form)

    def _open_id(self, player) -> None:
        player.send_form(
            ModalForm(
                title=self._text("minecraft_id", "title", "ID Discord", {}),
                controls=[
                    TextInput(
                        label=self._text("minecraft_id", "label", "Ton ID utilisateur Discord", {}),
                        placeholder=self._text("minecraft_id", "placeholder", "123456789012345678", {}),
                    )
                ],
                submit_button=self._text("minecraft_id", "submit_button", "Créer un code", {}),
                on_submit=lambda p, r: self.plugin.start_link(p, _first(r)),
            )
        )

    def show_code(self, player, code: str) -> None:
        minutes = max(1, self.plugin.settings.expiry_seconds // 60)
        context = {
            "code": code,
            "expiry_minutes": str(minutes),
            "verification_channel_id": self.plugin.settings.verification_channel_id,
            "server_name": self.plugin.settings.server_name,
        }
        content = self._text(
            "minecraft_code",
            "content",
            "§7Entre ce code dans le salon de vérification Discord :\n\n§l§e{code}§r\n\n§7Expire dans §f{expiry_minutes} min§7. Appuie sur §aVérifier §7ou utilise §f/verify §7dans Discord.",
            context,
        )
        player.send_form(
            MessageForm(
                title=self._text("minecraft_code", "title", "§l§aCode de vérification", context),
                content=content,
                button1=self._text("minecraft_code", "done_button", "§aTerminé", context),
                button2=self._text("minecraft_code", "close_button", "§8Fermer", context),
            )
        )
        chat = self._text(
            "minecraft_code",
            "chat_message",
            "§9§lDiscordLink§r §8»§r §fCode §e§l{code}§r §8• §7Entre-le dans Discord.",
            context,
        )
        if chat.strip():
            player.send_message(chat)

    def _confirm_unlink(self, player) -> None:
        player.send_form(
            MessageForm(
                title=self._text("minecraft_unlink", "title", "Délier Discord ?", {}),
                content=self._text("minecraft_unlink", "content", "§7Cela supprime le lien de compte et les rôles gérés par DiscordLink.", {}),
                button1=self._text("minecraft_unlink", "confirm_button", "§cDélier", {}),
                button2=self._text("minecraft_unlink", "cancel_button", "§8Annuler", {}),
                on_submit=lambda p, choice: self.plugin.unlink_player(p) if choice == 0 else self.open_player(p),
            )
        )

    def open_admin(self, player) -> None:
        status = self.plugin.integrations.detection_status()
        detected = "  ".join(f"§f{name} {'§a✓' if ok else '§8✕'}" for name, ok in status.items())
        gateway = "§aEn ligne" if self.plugin.gateway_running else "§cHors ligne"
        context = {
            "links": str(self.plugin.storage.count_links()),
            "pending": str(self.plugin.storage.count_pending()),
            "gateway": gateway,
            "integrations": detected,
        }
        form = ActionForm(
            title=self._text("minecraft_admin", "title", "§l§9Admin DiscordLink", context),
            content=self._text(
                "minecraft_admin",
                "content",
                "§7Liens §f{links}  §8•  §7En attente §f{pending}\n§7Bot §f{gateway}\n\n{integrations}",
                context,
            ),
        )
        form.add_button(
            self._text("minecraft_admin", "panel_button", "§dEnvoyer le panneau de vérification\n§7Publier le panneau Discord V2", context),
            on_click=lambda p: self.plugin.send_panel_from_minecraft(p, None),
        )
        form.add_button(
            self._text("minecraft_admin", "sync_button", "§bSynchroniser les joueurs en ligne\n§7Rafraîchir les rôles Discord gérés", context),
            on_click=lambda p: self.plugin.sync_all_online(notify_sender=p),
        )
        form.add_button(
            self._text("minecraft_admin", "reload_button", "§eRecharger la configuration", context),
            on_click=lambda p: self.plugin.reload_runtime(p),
        )
        form.add_button(self._text("minecraft_admin", "links_button", "§fComptes liés", context), on_click=self._open_links)
        form.add_button(self._text("minecraft_admin", "close_button", "§8Fermer", context))
        player.send_form(form)

    def _open_links(self, player) -> None:
        links = self.plugin.storage.list_links(50)
        content = "\n".join(f"§f{x.player_name} §8→ §7{x.discord_id}" for x in links) or "§7Aucun compte lié."
        player.send_form(
            MessageForm(
                title=self._text("minecraft_admin", "links_title", "Comptes liés", {}),
                content=content,
                button1=self._text("minecraft_admin", "back_button", "Retour", {}),
                button2=self._text("minecraft_admin", "close_button", "Fermer", {}),
                on_submit=lambda p, choice: self.open_admin(p) if choice == 0 else None,
            )
        )

    def _text(self, section: str, key: str, default: str, context: dict[str, str]) -> str:
        value = self.plugin.ui_value(section, key, default)
        return str(value).format_map(_SafeValues(context))

def _first(response) -> str:
    if isinstance(response, str):
        try:
            decoded = json.loads(response)
        except json.JSONDecodeError:
            return response.strip()
    else:
        decoded = response
    if isinstance(decoded, (list, tuple)) and decoded:
        return str(decoded[0]).strip()
    if isinstance(decoded, dict):
        for value in decoded.values():
            return str(value).strip()
    return ""

class _SafeValues(dict):
    def __missing__(self, key: str) -> str:
        return ""
