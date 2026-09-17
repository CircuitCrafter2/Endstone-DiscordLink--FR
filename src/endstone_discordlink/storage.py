from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LinkRecord:
    player_uuid: str
    xuid: str | None
    player_name: str
    discord_id: str
    verified_at: int


@dataclass(frozen=True)
class PendingResult:
    ok: bool
    reason: str
    discord_id: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    reason: str
    link: LinkRecord | None = None
    previous_link: LinkRecord | None = None


class LinkStorage:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS links (
                    player_uuid TEXT PRIMARY KEY,
                    xuid TEXT,
                    player_name TEXT NOT NULL,
                    discord_id TEXT NOT NULL UNIQUE,
                    verified_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_links_name ON links(player_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_links_xuid ON links(xuid);
                CREATE TABLE IF NOT EXISTS pending (
                    player_uuid TEXT PRIMARY KEY,
                    xuid TEXT,
                    player_name TEXT NOT NULL,
                    discord_id TEXT NOT NULL UNIQUE,
                    code_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS managed_roles (
                    player_uuid TEXT NOT NULL,
                    role_id TEXT NOT NULL,
                    PRIMARY KEY(player_uuid, role_id)
                );
                """
            )

    def get_link(self, player_uuid: str) -> LinkRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT player_uuid,xuid,player_name,discord_id,verified_at FROM links WHERE player_uuid=?",
                (player_uuid,),
            ).fetchone()
        return self._record(row)

    def get_link_by_discord(self, discord_id: str) -> LinkRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT player_uuid,xuid,player_name,discord_id,verified_at FROM links WHERE discord_id=?",
                (discord_id,),
            ).fetchone()
        return self._record(row)

    def find_link(self, identifier: str) -> LinkRecord | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT player_uuid,xuid,player_name,discord_id,verified_at FROM links
                   WHERE player_uuid=? OR xuid=? OR discord_id=? OR player_name=? COLLATE NOCASE
                   LIMIT 1""",
                (identifier, identifier, identifier, identifier),
            ).fetchone()
        return self._record(row)

    def get_managed_roles(self, player_uuid: str) -> set[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT role_id FROM managed_roles WHERE player_uuid=?",
                (player_uuid,),
            ).fetchall()
        return {str(row[0]) for row in rows if row and str(row[0]).strip()}

    def set_managed_roles(self, player_uuid: str, roles: set[str]) -> None:
        clean = {str(role).strip() for role in roles if str(role).strip()}
        with self._connect() as db:
            db.execute("DELETE FROM managed_roles WHERE player_uuid=?", (player_uuid,))
            db.executemany(
                "INSERT OR IGNORE INTO managed_roles(player_uuid,role_id) VALUES(?,?)",
                ((player_uuid, role) for role in sorted(clean)),
            )

    def clear_managed_roles(self, player_uuid: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM managed_roles WHERE player_uuid=?", (player_uuid,))

    def count_links(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM links").fetchone()[0])

    def count_pending(self) -> int:
        now = int(time.time())
        with self._connect() as db:
            db.execute("DELETE FROM pending WHERE expires_at<=?", (now,))
            return int(db.execute("SELECT COUNT(*) FROM pending").fetchone()[0])

    def list_links(self, limit: int = 100) -> tuple[LinkRecord, ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT player_uuid,xuid,player_name,discord_id,verified_at FROM links ORDER BY player_name COLLATE NOCASE LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return tuple(self._record(row) for row in rows if row is not None)

    def touch_player(self, player_uuid: str, xuid: str | None, player_name: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE links SET xuid=?, player_name=?, updated_at=? WHERE player_uuid=?",
                (xuid, player_name, int(time.time()), player_uuid),
            )

    def create_pending(
        self,
        *,
        player_uuid: str,
        xuid: str | None,
        player_name: str,
        discord_id: str,
        code: str,
        expiry_seconds: int,
        cooldown_seconds: int,
    ) -> PendingResult:
        now = int(time.time())
        with self._connect() as db:
            linked = db.execute(
                "SELECT player_uuid FROM links WHERE discord_id=? AND player_uuid<>?",
                (discord_id, player_uuid),
            ).fetchone()
            if linked:
                return PendingResult(False, "discord-in-use")
            existing = db.execute(
                "SELECT created_at FROM pending WHERE player_uuid=?",
                (player_uuid,),
            ).fetchone()
            if existing and now - int(existing[0]) < cooldown_seconds:
                return PendingResult(False, "cooldown")
            other = db.execute(
                "SELECT player_uuid FROM pending WHERE discord_id=? AND player_uuid<>? AND expires_at>?",
                (discord_id, player_uuid, now),
            ).fetchone()
            if other:
                return PendingResult(False, "discord-pending")
            db.execute("DELETE FROM pending WHERE expires_at<=?", (now,))
            db.execute(
                """INSERT INTO pending(player_uuid,xuid,player_name,discord_id,code_hash,created_at,expires_at,attempts)
                   VALUES(?,?,?,?,?,?,?,0)
                   ON CONFLICT(player_uuid) DO UPDATE SET
                     xuid=excluded.xuid,player_name=excluded.player_name,discord_id=excluded.discord_id,
                     code_hash=excluded.code_hash,created_at=excluded.created_at,expires_at=excluded.expires_at,attempts=0""",
                (
                    player_uuid,
                    xuid,
                    player_name,
                    discord_id,
                    _hash_code(code),
                    now,
                    now + expiry_seconds,
                ),
            )
        return PendingResult(True, "created", discord_id)

    def cancel_pending(self, player_uuid: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM pending WHERE player_uuid=?", (player_uuid,))

    def verify_pending_by_discord(self, discord_id: str, code: str, max_attempts: int) -> VerificationResult:
        now = int(time.time())
        with self._connect() as db:
            row = db.execute(
                "SELECT player_uuid,xuid,player_name,discord_id,code_hash,expires_at,attempts FROM pending WHERE discord_id=?",
                (discord_id,),
            ).fetchone()
            if row is None:
                return VerificationResult(False, "missing")
            player_uuid, xuid, player_name, pending_discord_id, code_hash, expires_at, attempts = row
            if int(expires_at) <= now:
                db.execute("DELETE FROM pending WHERE player_uuid=?", (player_uuid,))
                return VerificationResult(False, "expired")
            if int(attempts) >= max_attempts:
                db.execute("DELETE FROM pending WHERE player_uuid=?", (player_uuid,))
                return VerificationResult(False, "attempts")
            if _hash_code(code) != code_hash:
                attempts = int(attempts) + 1
                if attempts >= max_attempts:
                    db.execute("DELETE FROM pending WHERE player_uuid=?", (player_uuid,))
                    return VerificationResult(False, "attempts")
                db.execute("UPDATE pending SET attempts=? WHERE player_uuid=?", (attempts, player_uuid))
                return VerificationResult(False, "invalid")
            collision = db.execute(
                "SELECT player_uuid FROM links WHERE discord_id=? AND player_uuid<>?",
                (pending_discord_id, player_uuid),
            ).fetchone()
            if collision:
                db.execute("DELETE FROM pending WHERE player_uuid=?", (player_uuid,))
                return VerificationResult(False, "discord-in-use")
            previous_row = db.execute(
                "SELECT player_uuid,xuid,player_name,discord_id,verified_at FROM links WHERE player_uuid=?",
                (player_uuid,),
            ).fetchone()
            db.execute(
                """INSERT INTO links(player_uuid,xuid,player_name,discord_id,verified_at,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(player_uuid) DO UPDATE SET
                     xuid=excluded.xuid,player_name=excluded.player_name,discord_id=excluded.discord_id,
                     verified_at=excluded.verified_at,updated_at=excluded.updated_at""",
                (player_uuid, xuid, player_name, pending_discord_id, now, now),
            )
            db.execute("DELETE FROM pending WHERE player_uuid=?", (player_uuid,))
        link = LinkRecord(str(player_uuid), xuid, str(player_name), str(pending_discord_id), now)
        previous_link = self._record(previous_row)
        return VerificationResult(True, "verified", link, previous_link)

    def unlink(self, identifier: str) -> LinkRecord | None:
        record = self.find_link(identifier)
        if record is None:
            return None
        with self._connect() as db:
            db.execute("DELETE FROM links WHERE player_uuid=?", (record.player_uuid,))
            db.execute("DELETE FROM pending WHERE player_uuid=?", (record.player_uuid,))
        return record

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @staticmethod
    def _record(row: tuple | None) -> LinkRecord | None:
        if row is None:
            return None
        return LinkRecord(str(row[0]), row[1], str(row[2]), str(row[3]), int(row[4]))


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
