"""SQLite-хранилище позиций (aiosqlite)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite

STATUS_OPEN = "OPEN"
STATUS_WIN = "RESOLVED_WIN"
STATUS_LOSE = "RESOLVED_LOSE"
STATUS_CANCELLED = "CANCELLED"

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id      TEXT NOT NULL,
    token_id       TEXT NOT NULL,
    condition_id   TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    entry_price    REAL NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('OPEN','RESOLVED_WIN','RESOLVED_LOSE','CANCELLED')),
    created_at     TEXT NOT NULL,
    end_date       TEXT NOT NULL,
    event_title    TEXT,
    question       TEXT,
    event_slug     TEXT,
    market_slug    TEXT,
    shares         REAL NOT NULL DEFAULT 0,
    cost_usdc      REAL NOT NULL DEFAULT 0,
    payout_usdc    REAL,
    mode           TEXT NOT NULL,
    order_id       TEXT,
    closed_at      TEXT,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_condition ON positions(condition_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_positions_open_token ON positions(token_id) WHERE status = 'OPEN';
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Position:
    id: int
    market_id: str
    token_id: str
    condition_id: str
    outcome: str
    entry_price: float
    status: str
    created_at: datetime
    end_date: datetime
    event_title: str
    question: str
    event_slug: str
    market_slug: str
    shares: float
    cost_usdc: float
    payout_usdc: float | None
    mode: str
    order_id: str | None
    closed_at: datetime | None
    note: str | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Position":
        d = dict(row)
        return cls(
            id=d["id"],
            market_id=d["market_id"],
            token_id=d["token_id"],
            condition_id=d["condition_id"],
            outcome=d["outcome"],
            entry_price=d["entry_price"],
            status=d["status"],
            created_at=from_iso(d["created_at"]),
            end_date=from_iso(d["end_date"]),
            event_title=d["event_title"] or "",
            question=d["question"] or "",
            event_slug=d["event_slug"] or "",
            market_slug=d["market_slug"] or "",
            shares=d["shares"],
            cost_usdc=d["cost_usdc"],
            payout_usdc=d["payout_usdc"],
            mode=d["mode"],
            order_id=d["order_id"],
            closed_at=from_iso(d["closed_at"]) if d["closed_at"] else None,
            note=d["note"],
        )

    @property
    def pnl(self) -> float:
        if self.status in (STATUS_WIN, STATUS_LOSE):
            return (self.payout_usdc or 0.0) - self.cost_usdc
        return 0.0


@dataclass
class Stats:
    total: int
    open: int
    wins: int
    losses: int
    cancelled: int
    invested: float
    payout: float

    @property
    def pnl(self) -> float:
        return self.payout - self.invested

    @property
    def win_rate(self) -> float:
        done = self.wins + self.losses
        return self.wins / done * 100 if done else 0.0


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def count_open(self) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status = ?", (STATUS_OPEN,)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def insert_position(self, **fields: Any) -> int:
        fields.setdefault("status", STATUS_OPEN)
        fields.setdefault("created_at", to_iso(utcnow()))
        if isinstance(fields.get("end_date"), datetime):
            fields["end_date"] = to_iso(fields["end_date"])
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = await self.conn.execute(
            f"INSERT INTO positions ({cols}) VALUES ({marks})", tuple(fields.values())
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def update_position(self, position_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self.conn.execute(
            f"UPDATE positions SET {sets} WHERE id = ?", (*fields.values(), position_id)
        )
        await self.conn.commit()

    async def close_position(
        self, position_id: int, status: str, payout: float | None = None, note: str | None = None
    ) -> bool:
        """Переводит OPEN-позицию в финальный статус. Возвращает False, если она уже закрыта."""
        cur = await self.conn.execute(
            "UPDATE positions SET status = ?, payout_usdc = ?, closed_at = ?, "
            "note = COALESCE(?, note) WHERE id = ? AND status = ?",
            (status, payout, to_iso(utcnow()), note, position_id, STATUS_OPEN),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_position(self, position_id: int) -> Position | None:
        async with self.conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)) as cur:
            row = await cur.fetchone()
        return Position.from_row(row) if row else None

    async def get_open_positions(self) -> list[Position]:
        async with self.conn.execute(
            "SELECT * FROM positions WHERE status = ? ORDER BY end_date ASC", (STATUS_OPEN,)
        ) as cur:
            rows = await cur.fetchall()
        return [Position.from_row(r) for r in rows]

    async def known_condition_ids(self) -> set[str]:
        """Все рынки, по которым уже был сигнал/сделка — повторно их не предлагаем."""
        async with self.conn.execute("SELECT DISTINCT condition_id FROM positions") as cur:
            rows = await cur.fetchall()
        return {r[0] for r in rows}

    async def stats(self) -> Stats:
        async with self.conn.execute(
            """
            SELECT
                COUNT(*),
                SUM(status = 'OPEN'),
                SUM(status = 'RESOLVED_WIN'),
                SUM(status = 'RESOLVED_LOSE'),
                SUM(status = 'CANCELLED'),
                COALESCE(SUM(CASE WHEN status IN ('RESOLVED_WIN','RESOLVED_LOSE') THEN cost_usdc END), 0),
                COALESCE(SUM(CASE WHEN status IN ('RESOLVED_WIN','RESOLVED_LOSE') THEN payout_usdc END), 0)
            FROM positions
            """
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        return Stats(
            total=int(row[0] or 0),
            open=int(row[1] or 0),
            wins=int(row[2] or 0),
            losses=int(row[3] or 0),
            cancelled=int(row[4] or 0),
            invested=float(row[5] or 0),
            payout=float(row[6] or 0),
        )
