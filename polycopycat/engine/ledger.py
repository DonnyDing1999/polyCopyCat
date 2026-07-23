"""sqlite 账本：信号、订单、持仓与已实现盈亏。

- 信号表的 trade_key 唯一约束天然提供重启幂等：进程重启后同一笔
  目标成交不会被处理第二次。
- 持仓与盈亏在纸面模式下由本账本权威记录；实盘模式下成交以对账为准。
- 引擎线程、对账线程会并发读写，所有写操作走同一把锁。
"""

from __future__ import annotations

import logging
import re
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
    detail TEXT DEFAULT '',
    source TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_target ON events(target);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts REAL NOT NULL
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


@dataclass(frozen=True)
class ChannelQuality:
    """单个信号通道（stream/poll/backfill）的成交表现。"""

    source: str
    n_fills: int
    median_delay_s: float
    avg_delay_s: float


@dataclass(frozen=True)
class ExecutionQuality:
    """执行质量汇总：延迟、相对目标的价差、成交完成度（见 execution_quality）。"""

    n_fills: int = 0
    median_delay_s: float = 0.0
    avg_delay_s: float = 0.0
    max_delay_s: float = 0.0
    avg_price_gap: float = 0.0   # 平均劣化（正 = 比目标价差）
    slippage_cost: float = 0.0   # Σ(价差 × 成交量)，多付的钱
    full_fills: int = 0
    retried_fills: int = 0
    channels: tuple[ChannelQuality, ...] = ()  # 按信号通道拆分（哪条路真正促成了成交）


@dataclass(frozen=True)
class TargetReport:
    """单个目标的跟单归因：信号归属 + 可归因的已实现盈亏。"""

    target: str
    executed: int = 0
    filtered: int = 0
    skipped: int = 0
    risk_blocked: int = 0
    netted: int = 0
    no_fill: int = 0
    other: int = 0            # received/error 等其余状态
    bought_notional: float = 0.0   # 跟随该目标累计买入金额（下过的注）
    realized_pnl: float = 0.0      # 可归因的已实现盈亏（卖出跟随平掉的部分）
    settle_pnl: float = 0.0        # 结算/强平盈亏按建仓成本占比分摊到本目标的部分

    @property
    def total_pnl(self) -> float:
        """本目标合计盈亏：卖出跟随平仓 + 结算/强平归因。"""
        return self.realized_pnl + self.settle_pnl

    @property
    def total_signals(self) -> int:
        return (
            self.executed + self.filtered + self.skipped
            + self.risk_blocked + self.netted + self.no_fill + self.other
        )

    @property
    def followable_ratio(self) -> float:
        """执行占信号总数的比例：这个目标有多少动作真的跟得上。"""
        total = self.total_signals
        return self.executed / total if total else 0.0


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
            # 老账本迁移：signals 补 source 列（CREATE IF NOT EXISTS 不会给已有表加列）
            columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(signals)")}
            if "source" not in columns:
                self._conn.execute("ALTER TABLE signals ADD COLUMN source TEXT DEFAULT ''")

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
                    title, outcome, side, ref_price, ref_size, ref_notional, status, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    repr(trade.key), signal.received_at, trade.timestamp,
                    signal.target.address, trade.condition_id, trade.asset,
                    trade.title, trade.outcome, trade.side,
                    trade.price, trade.size, trade.notional, "received",
                    getattr(trade, "source", ""),
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

    def backfill_position_meta(self, meta: dict) -> int:
        """回填持仓表里缺失的 title / condition_id（成交推送偶尔缺元数据）。

        meta: {token_id: (title, condition_id)}；只覆盖空字段，非空的不动。
        返回实际更新的行数。
        """
        updated = 0
        with self._lock:
            for token_id, (title, condition_id) in meta.items():
                cur = self._conn.execute(
                    """UPDATE positions SET
                         title = CASE WHEN COALESCE(title, '') = '' THEN ? ELSE title END,
                         condition_id = CASE WHEN COALESCE(condition_id, '') = ''
                                             THEN ? ELSE condition_id END
                       WHERE token_id = ?
                         AND (COALESCE(title, '') = '' OR COALESCE(condition_id, '') = '')""",
                    (title or "", condition_id or "", token_id),
                )
                updated += cur.rowcount
        return updated

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

    def buy_builders(self, token_id: str) -> set[str]:
        """该 token 的建仓者：下过 BUY 且成交（status='executed'）的目标地址集合。

        对账强制离场兜底用——这些目标全都清仓了，才认定该跟的时机已过、强平自仓。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT target FROM signals "
                "WHERE token_id = ? AND side = 'BUY' AND status = 'executed'",
                (token_id,),
            ).fetchall()
        return {r["target"] for r in rows}

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

    def execution_quality(self) -> "ExecutionQuality":
        """执行质量：edge 被延迟和滑点吃掉多少（只统计有成交的跟单单）。

        延迟 = 订单落库时间 - 目标成交时间（端到端：发现 + 聚合窗口 + 定价 + 执行）；
        价差 = 我们的成交均价相对目标成交价的劣化（订单表 slippage 列，正 = 更差，
        买卖两侧已归一）；滑点成本 = Σ(价差 × 成交量)，即「跟得慢/盘口差」合计
        多付了多少钱。REDEEM（signal_id=0）天然不参与 join，不计入。
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT o.created_ts - s.trade_ts AS delay_s, s.source AS source,
                          o.slippage, o.filled_size, o.req_size, o.detail
                   FROM orders o JOIN signals s ON o.signal_id = s.id
                   WHERE o.side IN ('BUY', 'SELL') AND o.filled_size > 0"""
            ).fetchall()
        if not rows:
            return ExecutionQuality()
        delays = sorted(max(0.0, r["delay_s"]) for r in rows)
        gaps = [r["slippage"] for r in rows]
        by_source: dict[str, list[float]] = {}
        for r in rows:
            by_source.setdefault(r["source"] or "未知", []).append(max(0.0, r["delay_s"]))
        channels = tuple(
            ChannelQuality(
                source=source,
                n_fills=len(ds),
                median_delay_s=sorted(ds)[len(ds) // 2],
                avg_delay_s=sum(ds) / len(ds),
            )
            for source, ds in sorted(by_source.items(), key=lambda kv: -len(kv[1]))
        )
        return ExecutionQuality(
            n_fills=len(rows),
            median_delay_s=delays[len(delays) // 2],
            avg_delay_s=sum(delays) / len(delays),
            max_delay_s=delays[-1],
            avg_price_gap=sum(gaps) / len(gaps),
            slippage_cost=sum(r["slippage"] * r["filled_size"] for r in rows),
            full_fills=sum(1 for r in rows if r["filled_size"] >= r["req_size"] - 1e-9),
            retried_fills=sum(1 for r in rows if "重试" in (r["detail"] or "")),
            channels=channels,
        )

    def signal_source_counts(self) -> dict[str, int]:
        """各信号通道送来多少条（含被过滤的），衡量通道的真实贡献。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(NULLIF(source, ''), '未知') AS s, COUNT(*) AS n "
                "FROM signals GROUP BY s"
            ).fetchall()
        return {r["s"]: r["n"] for r in rows}

    def filter_reason_stats(self, top: int = 6) -> list[tuple[str, str, int]]:
        """被拦信号的原因分布（数字归一后聚类），回答「95 个 filtered 都是什么」。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, detail FROM signals "
                "WHERE status IN ('filtered', 'skipped') AND detail != ''"
            ).fetchall()
        counter: dict[tuple[str, str], int] = {}
        for r in rows:
            pattern = re.sub(r"\d[\d,.]*", "~", r["detail"])[:48]
            key = (r["status"], pattern)
            counter[key] = counter.get(key, 0) + 1
        ranked = sorted(counter.items(), key=lambda kv: -kv[1])[: max(1, int(top))]
        return [(status, pattern, n) for (status, pattern), n in ranked]

    # ---- 引擎状态（巡检暂停名单/计时等，重启后接着算而不是从零）----

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO state (key, value, updated_ts) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value = excluded.value, updated_ts = excluded.updated_ts""",
                (key, str(value), time.time()),
            )

    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    # ---- 事件（巡检/招募动作的持久档案，复盘「谁被停过、为什么」用）----

    def record_event(self, kind: str, target: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, kind, target, detail) VALUES (?,?,?,?)",
                (time.time(), kind, target, detail),
            )

    def target_event_summary(self) -> dict[str, dict]:
        """每个目标的事件摘要：被停次数、招募标记、最近一次事件。"""
        with self._lock:
            counts = self._conn.execute(
                """SELECT target,
                          SUM(CASE WHEN kind = 'health_pause' THEN 1 ELSE 0 END) AS pauses,
                          SUM(CASE WHEN kind = 'recruit' THEN 1 ELSE 0 END) AS recruits
                   FROM events GROUP BY target"""
            ).fetchall()
            latest = self._conn.execute(
                """SELECT e.target, e.kind, e.detail, e.ts FROM events e
                   JOIN (SELECT target, MAX(id) AS mid FROM events GROUP BY target) m
                     ON e.id = m.mid"""
            ).fetchall()
        summary: dict[str, dict] = {}
        for r in counts:
            summary[r["target"]] = {
                "pauses": r["pauses"], "recruits": r["recruits"],
                "last_kind": "", "last_detail": "", "last_ts": 0.0,
            }
        for r in latest:
            entry = summary.setdefault(
                r["target"],
                {"pauses": 0, "recruits": 0, "last_kind": "", "last_detail": "", "last_ts": 0.0},
            )
            entry.update(last_kind=r["kind"], last_detail=r["detail"], last_ts=r["ts"])
        return summary

    def recent_orders(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()

    def signal_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM signals GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def report_by_target(self) -> tuple[list[TargetReport], float, int]:
        """按目标拆分：每个目标的信号归属 + 已实现盈亏 + 结算/强平归因。

        返回 (每目标报告[按合计盈亏降序], 无法归因的盈亏, 对应订单数)。
        卖出跟随的盈亏经 orders→signals 干净归到目标；结算(REDEEM)与对账强平
        (side=SELL) 都是 signal_id=0、不直接挂目标，按该 token 各目标的建仓成本
        (BUY 成交额) 占比把 realized_pnl 分摊进各自的「结算归因」桶；查不到任何
        建仓 BUY 的（非跟单来源的仓）留在「未按目标归属」单列返回。
        """
        _KNOWN = {"executed", "filtered", "skipped", "risk_blocked", "netted", "no_fill"}
        with self._lock:
            sig_rows = self._conn.execute(
                "SELECT target, status, COUNT(*) AS n FROM signals GROUP BY target, status"
            ).fetchall()
            ord_rows = self._conn.execute(
                """SELECT s.target AS target,
                          COALESCE(SUM(o.realized_pnl), 0) AS realized,
                          COALESCE(SUM(CASE WHEN o.side = 'BUY' THEN o.notional ELSE 0 END), 0)
                            AS bought
                   FROM orders o JOIN signals s ON o.signal_id = s.id
                   GROUP BY s.target"""
            ).fetchall()
            # 各 token 的建仓成本按目标拆分：BUY 成交额（filled_size×avg_price），供结算/强平分摊
            build_rows = self._conn.execute(
                """SELECT o.token_id AS token_id, s.target AS target,
                          COALESCE(SUM(o.filled_size * o.avg_price), 0) AS cost
                   FROM orders o JOIN signals s ON o.signal_id = s.id
                   WHERE o.side = 'BUY' AND o.filled_size > 0
                   GROUP BY o.token_id, s.target"""
            ).fetchall()
            # 待归因的结算/强平单：signal_id=0 且有实际盈亏（REDEEM 结算 + 对账离场）
            settle_rows = self._conn.execute(
                "SELECT token_id, realized_pnl FROM orders "
                "WHERE signal_id = 0 AND realized_pnl != 0"
            ).fetchall()

        # token -> {target: 建仓成本}
        build_cost: dict[str, dict[str, float]] = {}
        for r in build_rows:
            build_cost.setdefault(r["token_id"], {})[r["target"]] = r["cost"]
        # 逐笔把结算/强平盈亏按建仓成本占比摊给各建仓目标；无建仓来源的留未归属
        settle_by_target: dict[str, float] = {}
        unattr_pnl = 0.0
        unattr_n = 0
        for r in settle_rows:
            costs = build_cost.get(r["token_id"])
            total = sum(costs.values()) if costs else 0.0
            if not costs or total <= 0:
                unattr_pnl += r["realized_pnl"]
                unattr_n += 1
                continue
            for target, cost in costs.items():
                settle_by_target[target] = (
                    settle_by_target.get(target, 0.0) + r["realized_pnl"] * cost / total
                )

        agg: dict[str, dict] = {}
        for r in sig_rows:
            bucket = agg.setdefault(r["target"], {"counts": {}, "bought": 0.0, "realized": 0.0})
            bucket["counts"][r["status"]] = r["n"]
        for r in ord_rows:
            bucket = agg.setdefault(r["target"], {"counts": {}, "bought": 0.0, "realized": 0.0})
            bucket["bought"] = r["bought"]
            bucket["realized"] = r["realized"]
        for target in settle_by_target:  # 只在结算归因里出现的目标也建桶（防御，一般已在信号里）
            agg.setdefault(target, {"counts": {}, "bought": 0.0, "realized": 0.0})

        reports: list[TargetReport] = []
        for target, bucket in agg.items():
            counts = bucket["counts"]
            other = sum(n for st, n in counts.items() if st not in _KNOWN)
            reports.append(TargetReport(
                target=target,
                executed=counts.get("executed", 0),
                filtered=counts.get("filtered", 0),
                skipped=counts.get("skipped", 0),
                risk_blocked=counts.get("risk_blocked", 0),
                netted=counts.get("netted", 0),
                no_fill=counts.get("no_fill", 0),
                other=other,
                bought_notional=bucket["bought"],
                realized_pnl=bucket["realized"],
                settle_pnl=settle_by_target.get(target, 0.0),
            ))
        reports.sort(key=lambda t: (t.total_pnl, t.bought_notional), reverse=True)
        return reports, unattr_pnl, unattr_n
