from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class IntegrationSnapshot:
    stoneperms_primary: str | None = None
    stoneperms_groups: set[str] = field(default_factory=set)
    pureperms_primary: str | None = None
    pureperms_groups: set[str] = field(default_factory=set)
    faction_name: str | None = None
    faction_rank: str | None = None
    balance: float | None = None
    detected: set[str] = field(default_factory=set)


class IntegrationResolver:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def snapshot(self, player) -> IntegrationSnapshot:
        out = IntegrationSnapshot()
        self._stoneperms(player, out)
        self._pureperms(player, out)
        self._factions(player, out)
        self._economy(player, out)
        return out

    def detection_status(self) -> dict[str, bool]:
        return {
            "StonePerms": self._service(("stoneperms.permissions.v1",)) is not None
            or self._plugin(("StonePerms", "stoneperms")) is not None,
            "PurePerms": self._service(("pureperms.permissions.v1", "pureperms", "kyromc.permissions.v1")) is not None
            or self._plugin(("PurePerms", "pureperms", "KyroMC", "kyromc")) is not None,
            "Factions": self._service(("factions.v1", "zfactions.v1", "kyromc.factions.v1")) is not None
            or self._plugin(("Factions", "factions", "zFactions", "zfactions", "KyroMC", "kyromc")) is not None,
            "BedrockEconomy": self._service(("bedrockeconomy.economy.v1", "bedrockeconomy", "economy.v1", "kyromc.economy.v1")) is not None
            or self._plugin(("BedrockEconomy", "bedrockeconomy", "KyroMC", "kyromc")) is not None,
        }

    def _stoneperms(self, player, out: IntegrationSnapshot) -> None:
        service = self._service(("stoneperms.permissions.v1",))
        if service is None:
            return
        try:
            out.stoneperms_primary = _text(service.get_primary_group(player))
            out.stoneperms_groups.update(_strings(service.get_groups(player)))
            if out.stoneperms_primary:
                out.stoneperms_groups.add(out.stoneperms_primary)
            out.detected.add("StonePerms")
        except Exception as exc:
            self.plugin.logger.warning(f"StonePerms integration lookup failed: {exc}")

    def _pureperms(self, player, out: IntegrationSnapshot) -> None:
        service = self._service(("pureperms.permissions.v1", "pureperms", "kyromc.permissions.v1"))
        roots = _objects(service) if service is not None else []
        candidate = self._plugin(("PurePerms", "pureperms", "KyroMC", "kyromc"))
        if candidate is not None:
            roots.extend(_objects(candidate))
        roots = _unique_objects(roots)
        if not roots:
            return
        primary = self._call(
            roots,
            (
                "get_primary_group",
                "get_player_group",
                "get_group",
                "get_user_group",
                "group_for",
                "getPrimaryGroup",
                "getPlayerGroup",
                "getGroup",
                "getUserGroup",
            ),
            player,
        )
        groups = self._call(
            roots,
            ("get_groups", "get_player_groups", "get_user_groups", "getGroups", "getPlayerGroups", "getUserGroups"),
            player,
        )
        data = self._call(
            roots,
            ("get_player_data", "get_user_data", "getPlayerData", "getUserData"),
            player,
        )
        if isinstance(data, dict):
            if primary is None:
                for key in ("primary_group", "primaryGroup", "group", "rank"):
                    if data.get(key) is not None:
                        primary = data.get(key)
                        break
            if groups is None:
                for key in ("groups", "group_names", "groupNames"):
                    if data.get(key) is not None:
                        groups = data.get(key)
                        break
        if primary is None and groups is None:
            mapped = _lookup_player_mapping(roots, player)
            if isinstance(mapped, dict):
                primary = mapped.get("primary_group", mapped.get("group", mapped.get("rank")))
                groups = mapped.get("groups")
            elif mapped is not None:
                primary = mapped
        name = _name(primary)
        if name:
            out.pureperms_primary = name
            out.pureperms_groups.add(name)
        out.pureperms_groups.update(_strings(groups))
        if out.pureperms_primary or out.pureperms_groups:
            out.detected.add("PurePerms")

    def _factions(self, player, out: IntegrationSnapshot) -> None:
        service = self._service(("factions.v1", "zfactions.v1", "kyromc.factions.v1"))
        roots = _objects(service) if service is not None else []
        candidate = self._plugin(("Factions", "factions", "zFactions", "zfactions", "KyroMC", "kyromc"))
        if candidate is not None:
            roots.extend(_objects(candidate))
        roots = _unique_objects(roots)
        if not roots:
            return
        faction = self._call(
            roots,
            (
                "get_player_faction",
                "get_faction_of",
                "get_faction",
                "faction_of",
                "get_faction_by_player",
                "getPlayerFaction",
                "getFactionOf",
                "getFaction",
                "getFactionByPlayer",
            ),
            player,
        )
        rank = self._call(
            roots,
            (
                "get_player_rank",
                "get_faction_rank",
                "get_rank",
                "get_role",
                "get_member_rank",
                "getPlayerRank",
                "getFactionRank",
                "getRank",
                "getRole",
                "getMemberRank",
            ),
            player,
        )
        data = self._call(roots, ("get_player_data", "get_member_data", "getPlayerData", "getMemberData"), player)
        if isinstance(data, dict):
            if faction is None:
                faction = data.get("faction", data.get("faction_name", data.get("factionName")))
            if rank is None:
                rank = data.get("rank", data.get("role", data.get("faction_rank", data.get("factionRank"))))
        out.faction_name = _name(faction)
        out.faction_rank = _name(rank)
        if out.faction_rank is None and faction is not None:
            out.faction_rank = _attr_name(faction, ("rank", "role", "member_role", "memberRank"))
        if out.faction_name or out.faction_rank:
            out.detected.add("Factions")

    def _economy(self, player, out: IntegrationSnapshot) -> None:
        service = self._service(("bedrockeconomy.economy.v1", "bedrockeconomy", "economy.v1", "kyromc.economy.v1"))
        roots = [service] if service is not None else []
        candidate = self._plugin(("BedrockEconomy", "bedrockeconomy", "KyroMC", "kyromc"))
        if candidate is not None:
            roots.extend(_objects(candidate))
        roots = [root for root in roots if root is not None]
        if not roots:
            return
        value = self._call(
            roots,
            ("get_balance", "get_player_balance", "get_money", "balance"),
            player,
        )
        if value is None:
            value = self._call(
                roots,
                ("get_balance", "get_player_balance", "get_money", "balance"),
                player.name,
            )
        try:
            if isinstance(value, dict):
                value = value.get("balance", value.get("money"))
            out.balance = float(value) if value is not None else None
        except (TypeError, ValueError):
            out.balance = None
        if out.balance is not None:
            out.detected.add("BedrockEconomy")

    def _service(self, names: Iterable[str]):
        for name in names:
            try:
                value = self.plugin.server.service_manager.load(name)
            except Exception:
                value = None
            if value is not None:
                return value
        return None

    def _plugin(self, names: Iterable[str]):
        manager = self.plugin.server.plugin_manager
        for name in names:
            try:
                value = manager.get_plugin(name)
            except Exception:
                value = None
            if value is not None and value is not self.plugin:
                return value
        return None

    @staticmethod
    def _call(roots: list[Any], names: tuple[str, ...], subject: Any):
        subjects = (
            subject,
            getattr(subject, "name", None),
            getattr(subject, "xuid", None),
            str(getattr(subject, "unique_id", "")),
        )
        for root in roots:
            for name in names:
                fn = getattr(root, name, None)
                if not callable(fn):
                    continue
                for arg in subjects:
                    if arg is None or arg == "":
                        continue
                    try:
                        return fn(arg)
                    except (TypeError, KeyError, LookupError, ValueError, AttributeError):
                        continue
                    except Exception:
                        continue
        return None


def _objects(root: Any) -> list[Any]:
    if root is None:
        return []
    values = [root]
    attributes = (
        "api",
        "service",
        "manager",
        "economy",
        "permissions",
        "factions",
        "pureperms",
        "user_data_mgr",
        "user_data_manager",
        "group_manager",
        "_api",
        "_service",
        "_manager",
        "_user_data_mgr",
        "_user_data_manager",
        "_group_manager",
    )
    accessors = (
        "get_api",
        "get_service",
        "get_manager",
        "get_user_data_mgr",
        "get_user_data_manager",
        "get_group_manager",
        "getApi",
        "getService",
        "getManager",
        "getUserDataMgr",
        "getUserDataManager",
        "getGroupManager",
    )
    index = 0
    while index < len(values) and index < 24:
        current = values[index]
        index += 1
        for name in attributes:
            try:
                value = getattr(current, name, None)
            except Exception:
                value = None
            if value is not None and value not in values:
                values.append(value)
        for name in accessors:
            try:
                fn = getattr(current, name, None)
                value = fn() if callable(fn) else None
            except Exception:
                value = None
            if value is not None and value not in values:
                values.append(value)
    return values


def _unique_objects(values: list[Any]) -> list[Any]:
    result = []
    for value in values:
        if value is not None and value not in result:
            result.append(value)
    return result


def _lookup_player_mapping(roots: list[Any], player: Any):
    keys = []
    for value in (getattr(player, "name", None), getattr(player, "xuid", None), str(getattr(player, "unique_id", ""))):
        if value is not None and str(value).strip():
            keys.append(str(value))
    attributes = (
        "player_groups",
        "user_groups",
        "assignments",
        "players",
        "users",
        "user_data",
        "player_data",
        "_player_groups",
        "_user_groups",
        "_assignments",
        "_players",
        "_users",
        "_user_data",
        "_player_data",
    )
    for root in roots:
        for name in attributes:
            try:
                mapping = getattr(root, name, None)
            except Exception:
                mapping = None
            if not isinstance(mapping, dict):
                continue
            folded = {str(k).casefold(): v for k, v in mapping.items()}
            for key in keys:
                if key in mapping:
                    return mapping[key]
                if key.casefold() in folded:
                    return folded[key.casefold()]
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _name(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        for key in ("name", "group", "rank", "role", "faction", "tag"):
            if key in value and value[key] is not None:
                return _text(value[key])
    direct = _attr_name(value, ("name", "group", "rank", "role", "tag", "display_name"))
    if direct:
        return direct
    for name in ("get_name", "getName", "get_group", "getGroup", "get_rank", "getRank", "get_role", "getRole"):
        try:
            fn = getattr(value, name, None)
            result = fn() if callable(fn) else None
        except Exception:
            result = None
        text = _text(result)
        if text:
            return text
    text = _text(value)
    if text and not (text.startswith("<") and " object at 0x" in text):
        return text
    return None


def _attr_name(value: Any, names: tuple[str, ...]) -> str | None:
    for name in names:
        try:
            candidate = getattr(value, name, None)
        except Exception:
            candidate = None
        if candidate is not None and not callable(candidate):
            text = _text(candidate)
            if text:
                return text
    return None


def _strings(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value.strip()} if value.strip() else set()
    if isinstance(value, dict):
        value = value.keys()
    try:
        result = set()
        for item in value:
            name = _name(item)
            if name:
                result.add(name)
        return result
    except TypeError:
        name = _name(value)
        return {name} if name else set()
