"""sqlite 账本：信号、订单、持仓与已实现盈亏。

- 信号表的 trade_key 唯一约束天然提供重启幂等：进程重启后同一笔
  目标成交不会被处理第二次。
- 持仓与盈亏在纸面模式下由本账本权威记录；实盘模式下成交以对账为准。
- 引擎线程、对账线程会并发读写，所有写操作走同一把锁。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .signals import OrderIntent, Signal

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_key TEXT UNIQUE NOT NULL,
    created_ts REAL NOT NULL,
    trade_ts INTEGER NOT NULL,
    target TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    title TEXT,
    outcome TEXT,
    side TEXT NOT NULL,
    ref_price REAL NOT NULL,
    ref_size REAL NOT NULL,
    ref_notional REAL NOT NULL,
    status TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL,
    created_ts REAL NOT NULL,
    mode TEXT NOT NULL,
    token_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    title TEXT,
    outcome TEXT,
    side TEXT NOT NULL,
    limit_price REAL NOT NULL,
    req_size REAL NOT NULL,
    filled_size REAL NOT NULL,
    avg_price REAL NOT NULL,
    notional REAL NOT NULL,
    slippage REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS positions (
    token_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    title TEXT,
    outcome TEXT,
    size REAL NOT NULL,
    avg_cost REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_ts);
CREATE INDEX IF NOT EXISTS idx_positions_condition ON positions(condition_id);
"""


@dataclass(frozen=True)
class PositionRow:
    token_id: str
    condition_id: str
    title: str
    outcome: str
    size: float
    avg_cost: float
    realized_pnl: float

    @property
    def cost(self) -> float:
        return self.size * self.avg_cost


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 信号 ----

    def record_signal(self, signal: Signal) -> tuple[int | None, bool]:
        """落一条信号，返回 (id, 是否新信号)；重复 trade_key 返回 (已有id, False)。"""
        trade = signal.trade
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO signals
                   (trade_key, created_ts, trade_ts, target, condition_id, token_id,
                    title, outcome, side, ref_price, ref_size, ref_notional, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    repr(trade.key), signal.received_at, trade.timestamp,
                    signal.target.address, trade.condition_id, trade.asset,
                    trade.title, trade.outcome, trade.side,
                    trade.price, trade.size, trade.notional, "received",
                ),
            )
            if cur.rowcount == 0:
                row = self._conn.execute(
                    "SELECT id FROM signals WHERE trade_key = ?", (repr(trade.key),)
                ).fetchone()
                return (row["id"] if row else None), False
            return cur.lastrowid, True

    def update_signal(self, signal_id: int, status: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE signals SET status = ?, detail = ? WHERE id = ?",
                (status, detail, signal_id),
            )

    # ---- 订单与持仓 ----

    def record_order(
        self,
        signal_id: int,
        intent: OrderIntent,
        *,
        mode: str,
        status: str,
        filled_size: float = 0.0,
        avg_price: float = 0.0,
        slippage: float = 0.0,
        detail: str = "",
        apply_fill: bool = True,
    ) -> float:
        """落一条订单记录；有成交且 apply_fill 时同步更新持仓，返回本单已实现盈亏。

        实盘提交后成交未知时传 apply_fill=False，持仓交给对账更新。
        """
        realized = 0.0
        with self._lock:
            if filled_size > 0 and apply_fill:
                realized = self._apply_fill_locked(intent, filled_size, avg_price)
            self._conn.execute(
                """INSERT INTO orders
                   (signal_id, created_ts, mode, token_id, condition_id, title, outcome,
                    side, limit_price, req_size, filled_size, avg_price, notional,
                    slippage, realized_pnl, status, detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    signal_id, time.time(), mode, intent.token_id, intent.condition_id,
                    intent.title, intent.outcome, intent.side, intent.limit_price,
                    intent.size, filled_size, avg_price, filled_size * avg_price,
                    slippage, realized, status, detail,
                ),
            )
        return realized

    def _apply_fill_locked(self, intent: OrderIntent, filled: float, price: float) -> float:
        row = self._conn.execute(
            "SELECT size, avg_cost, realized_pnl FROM positions WHERE token_id = ?",
            (intent.token_id,),
        ).fetchone()
        size, avg_cost, realized_total = (
            (row["size"], row["avg_cost"], row["realized_pnl"]) if row else (0.0, 0.0, 0.0)
        )
        realized = 0.0
        if intent.side == "BUY":
            new_size = size + filled
            avg_cost = (size * avg_cost + filled * price) / new_size if new_size > 0 else 0.0
            size = new_size
        else:
            if filled > size + 1e-9:
                logger.warning(
                    "卖出量 %.2f 超过账本持仓 %.2f（token %s），按持仓全量计",
                    filled, size, intent.token_id,
                )
                filled = size
            realized = filled * (price - avg_cost)
            size = max(0.0, size - filled)
            if size <= 1e-9:
                size = 0.0
        self._conn.execute(
            """INSERT INTO positions
               (token_id, condition_id, title, outcome, size, avg_cost, realized_pnl, updated_ts)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(token_id) DO UPDATE SET
                 size = excluded.size, avg_cost = excluded.avg_cost,
                 realized_pnl = excluded.realized_pnl, updated_ts = excluded.updated_ts,
                 title = excluded.title, outcome = excluded.outcome""",
            (
                intent.token_id, intent.condition_id, intent.title, intent.outcome,
                round(size, 9), round(avg_cost, 9), round(realized_total + realized, 9),
                time.time(),
            ),
        )
        return realized

    def settle_position(self, token_id: str, settle_price: float, *, mode: str) -> float | None:
        """市场结算后把持仓按结算价（赢 1.0 / 输 0.0）自动入账并清仓。

        记一条 side=REDEEM、status=settled 的订单行（signal_id=0，非信号驱动），
        使 report 的已实现盈亏统计如实包含结算损益。返回本次已实现盈亏；
        无持仓返回 None。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM positions WHERE token_id = ? AND size > 0", (token_id,)
            ).fetchone()
            if row is None:
                return None
            size, avg_cost = row["size"], row["avg_cost"]
            realized = round(size * (settle_price - avg_cost), 9)
            now = time.time()
            self._conn.execute(
                """UPDATE positions SET size = 0, realized_pnl = realized_pnl + ?,
                   updated_ts = ? WHERE token_id = ?""",
                (realized, now, token_id),
            )
            self._conn.execute(
                """INSERT INTO orders
                   (signal_id, created_ts, mode, token_id, condition_id, title, outcome,
                    side, limit_price, req_size, filled_size, avg_price, notional,
                    slippage, realized_pnl, status, detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    0, now, mode, token_id, row["condition_id"], row["title"], row["outcome"],
                    "REDEEM", settle_price, size, size, settle_price, size * settle_price,
                    0.0, realized, "settled", "市场已结算，按结算价自动入账",
                ),
            )
        return realized

    def sync_positions(self, positions) -> None:
        """实盘对账：用 Data API 持仓快照整体覆盖持仓表。

        实盘下单响应不含逐档成交明细（record_order 用 apply_fill=False），
        持仓的权威来源是链上/API 快照，定期整表覆盖。
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM positions")
                now = time.time()
                self._conn.executemany(
                    """INSERT INTO positions
                       (token_id, condition_id, title, outcome, size, avg_cost,
                        realized_pnl, updated_ts)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    [
                        (
                            p.asset, p.condition_id, p.title, p.outcome,
                            p.size, p.avg_price, p.realized_pnl, now,
                        )
                        for p in positions
                        if p.size > 0
                    ],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ---- 查询 ----

    def position_size(self, token_id: str) -> float:
        row = self._conn.execute(
            "SELECT size FROM positions WHERE token_id = ?", (token_id,)
        ).fetchone()
        return row["size"] if row else 0.0

    def market_cost(self, condition_id: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(size * avg_cost), 0) AS c FROM positions WHERE condition_id = ?",
            (condition_id,),
        ).fetchone()
        return row["c"]

    def total_cost(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(size * avg_cost), 0) AS c FROM positions"
        ).fetchone()
        return row["c"]

    def realized_pnl_since(self, ts: float) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS p FROM orders WHERE created_ts >= ?",
            (ts,),
        ).fetchone()
        return row["p"]

    def realized_pnl_total(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS p FROM orders"
        ).fetchone()
        return row["p"]

    def positions(self, include_empty: bool = False) -> list[PositionRow]:
        sql = "SELECT * FROM positions"
        if not include_empty:
            sql += " WHERE size > 0"
        sql += " ORDER BY updated_ts DESC"
        return [
            PositionRow(
                token_id=r["token_id"], condition_id=r["condition_id"],
                title=r["title"] or "", outcome=r["outcome"] or "",
                size=r["size"], avg_cost=r["avg_cost"], realized_pnl=r["realized_pnl"],
            )
            for r in self._conn.execute(sql).fetchall()
        ]

    def recent_orders(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()

    def signal_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM signals GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
