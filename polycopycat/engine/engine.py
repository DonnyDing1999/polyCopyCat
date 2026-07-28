"""跟单引擎主体：消费信号队列，串起过滤 → 聚合 → 计算 → 风控 → 执行 → 记账 → 通知。

引擎跑在自己的线程里，watcher/stream 线程只往队列里塞信号，
慢速的 CLOB 请求不会拖住行情监控。任何一笔信号处理抛异常都只
影响该笔（记为 error），引擎线程本身永不退出。

M3 聚合：收到首笔信号后先等一个窗口（aggregate.window_s，默认 2s），
把窗口内「同目标 + 同 token + 同方向」的碎片成交合并成一笔等效成交
再走后续管道；窗口内多目标对同一 token 的反向信号可轧差，只执行
净头寸。窗口设 0 即退化为逐笔跟单。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import queue
import threading
import time
from pathlib import Path

from ..data_api import DataApiClient, DataApiError
from ..models import Trade
from .aggregate import MergedGroup, PendingSignal, group_key, merge_pending
from .clob import ClobError, ClobReadClient, MarketInfo
from .config import EngineConfig, TargetConfig
from .executor import ExecutionResult
from .ledger import Ledger, PositionRow
from .mirror import TargetMirror
from .notify import Notifier
from .risk import RiskGate
from .signals import OrderIntent, Signal, SignalFilter
from .sizing import SIZE_STEP, floor_to, plan_buy, sell_limit_price

logger = logging.getLogger(__name__)

_STOP = object()


def _short(text: str) -> str:
    return f"{text[:6]}…{text[-4:]}" if len(text) > 12 else text


def _recruited_path(config: EngineConfig) -> Path:
    return Path(config.ledger_path).parent / "recruited.json"


@dataclasses.dataclass(frozen=True)
class _RosterCandidate:
    """名录条目（Verdict.to_dict() + evaluated_at 的 dict）→ 招募候选。

    只取 _recruit 用到的两个字段，好让招募既能吃 Verdict 也能吃名录 dict，
    而不必改 Verdict 类或名录的返回结构。老行缺 score 时兜 0：自然被分数
    门槛挡下，不会因为字段残缺被误招。
    """

    address: str
    score: float

    @classmethod
    def from_entry(cls, entry: dict) -> "_RosterCandidate":
        try:
            score = float(entry.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        return cls(address=str(entry.get("address") or "").lower(), score=score)


def merge_recruited_targets(config: EngineConfig) -> list[str]:
    """启动时把历史自动招募的目标（recruited.json）并回 targets。

    引擎运行中招募的目标只活在内存里，靠这个文件在重启后存续；用户
    配置文件永远不被改写。返回并入的地址列表。
    """
    path = _recruited_path(config)
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读取招募档案 %s 失败，忽略: %s", path, exc)
        return []
    if not isinstance(entries, list):
        return []
    existing = {t.address for t in config.targets}
    blocked = set(config.health.recruit_blocklist)
    added: list[str] = []
    dropped: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if len(config.targets) >= config.health.recruit_max_targets:
            break
        try:
            target = TargetConfig(
                address=str(entry.get("address", "")),
                ratio=entry.get("ratio"),
                max_per_trade_usdc=entry.get("max_per_trade_usdc"),
            )
        except Exception:  # noqa: BLE001 —— 单条损坏不拖累其余
            continue
        if target.address in blocked:
            dropped.append(target.address)
            continue
        if target.address in existing:
            continue
        config.targets.append(target)
        existing.add(target.address)
        added.append(target.address)
    if added:
        logger.info("已并回 %d 个历史招募目标: %s", len(added), ", ".join(_short(a) for a in added))
    if dropped:
        logger.info("招募档案里 %d 个地址在黑名单，已剔除: %s", len(dropped), ", ".join(_short(a) for a in dropped))
    return added


@dataclasses.dataclass
class _Planned:
    """完成仓位计算、等待风控与执行的一组信号。"""

    group: MergedGroup
    intent: OrderIntent
    market: MarketInfo


class CopyEngine:
    def __init__(
        self,
        config: EngineConfig,
        *,
        clob: ClobReadClient,
        ledger: Ledger,
        executor,
        notifier: Notifier,
        mirror: TargetMirror | None = None,
        data_client: DataApiClient | None = None,
        own_address: str | None = None,
        on_new_target=None,  # 招募新目标后的回调（cmd_run 用它让 watcher/stream 开始盯新地址）
    ) -> None:
        self.config = config
        self._clob = clob
        self._ledger = ledger
        self._executor = executor
        self._notifier = notifier
        self._mirror = mirror if mirror is not None else TargetMirror()
        self._data = data_client   # 对账数据源；None 则不启动对账线程
        self._own_address = own_address  # 实盘时自己的资金地址，用于持仓对账
        self._filter = SignalFilter(config.filters)
        self._risk = RiskGate(config.risk, ledger)
        self._targets = {t.address: t for t in config.targets}
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._reconcile_thread: threading.Thread | None = None
        self._reconcile_stop = threading.Event()
        self._redeem_notified: set[str] = set()
        # 强制离场两轮确认的待确认 token（内存态，不持久化；重启后重新数两轮可接受）
        self._pending_force_exit: set[str] = set()
        self._health_paused: set[str] = set()   # 由健康巡检暂停的目标（区别于手动 paused）
        # 巡检/发现计时用墙钟并持久化在账本 state 表：重启接着算，不从零
        self._last_health_check = time.time()
        self._last_discover = time.time()
        # 票池累积器：stream 线程逐笔写内存增量，发现线程周期性批量落库（绝不逐笔写库）
        self._universe_accum: dict[str, dict] = {}
        self._universe_lock = threading.Lock()
        self._eval_budget = 0.0                 # 涓流评估的小数预算累加器（支持 <1/min 速率）
        self._last_universe_flush = time.time()   # stream 增量落库的 ~10 分钟节拍（独立于每日刷新）
        self._last_discover_notify = time.time()  # 每小时摘要限流（内存态，重启延后首播 ≤1h，防刷屏）
        self._discover_new_eligible: dict = {}  # 本小时新合格者（地址→Verdict，供摘要与招募）
        self._on_new_target = on_new_target
        # 标记哪些目标是自动招募的（重启后从档案恢复标记，保存时不丢历史）
        self._recruited: dict[str, dict] = {}
        recruited_file = _recruited_path(config)
        if recruited_file.exists():
            try:
                for entry in json.loads(recruited_file.read_text(encoding="utf-8")) or []:
                    address = str(entry.get("address", "")).lower()
                    if address in self._targets:
                        self._recruited[address] = entry
            except (OSError, ValueError):
                pass
        self._load_persisted_state()

    def _load_persisted_state(self) -> None:
        """从账本恢复巡检暂停名单与巡检/发现计时（重启不清、不重置）。"""
        now = time.time()

        def load_ts(key: str) -> float:
            raw = self._ledger.get_state(key)
            try:
                ts = float(raw)
            except (TypeError, ValueError):
                return now  # 没有记录：从现在起算（首次运行语义不变）
            return min(ts, now)  # 防未来时间戳

        self._last_health_check = load_ts("health_last_check_ts")
        self._last_discover = load_ts("discover_last_run_ts")

        raw = self._ledger.get_state("health_paused")
        try:
            paused = json.loads(raw) if raw else []
        except ValueError:
            paused = []
        restored = []
        for address in paused:
            target = self._targets.get(str(address).lower())
            if target is None or target.paused:
                continue  # 目标已移除或本就手动暂停：不越权
            target.paused = True
            self._health_paused.add(target.address)
            restored.append(target.address)
        if restored:
            logger.info(
                "已恢复 %d 个巡检暂停状态: %s",
                len(restored), ", ".join(_short(a) for a in restored),
            )

    def _persist_health_paused(self) -> None:
        self._ledger.set_state("health_paused", json.dumps(sorted(self._health_paused)))

    # ---- 对外接口 ----

    def submit(self, trade: Trade) -> None:
        """接收一笔目标成交（作为 watcher 的 on_trade 回调，多线程安全）。"""
        target = self._targets.get(trade.proxy_wallet)
        if target is None:
            return
        self._queue.put(Signal(trade=trade, target=target, received_at=time.time()))

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("引擎已经启动过")
        if self._data is not None:
            logger.info("正在初始化目标持仓镜像……")
            self.reconcile_once()
            self._reconcile_thread = threading.Thread(
                target=self._reconcile_loop, name="polycopycat-reconcile", daemon=True
            )
            self._reconcile_thread.start()
            if self.config.health.discover_interval_s > 0:
                threading.Thread(
                    target=self._discover_loop, name="polycopycat-discover", daemon=True
                ).start()
        self._thread = threading.Thread(target=self._loop, name="polycopycat-engine", daemon=True)
        self._thread.start()
        logger.info(
            "跟单引擎已启动（%s 模式，%d 个目标，滑点上限 %.3f，聚合窗口 %.1fs）",
            self.config.mode, len(self._targets), self.config.execution.slippage_cap,
            self.config.aggregate.window_s,
        )

    def stop(self, timeout: float = 30.0) -> None:
        """处理完队列中已有信号后停止。"""
        self._reconcile_stop.set()
        if self._reconcile_thread is not None:
            self._reconcile_thread.join(timeout=5)
        if self._thread is None:
            return
        self._queue.put(_STOP)
        self._thread.join(timeout=timeout)

    def drain(self) -> None:
        """等队列清空（测试与验证用）。"""
        self._queue.join()

    # ---- 主循环 ----

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                return
            batch = [item]
            stop_after = self._collect_window(batch)
            try:
                self._process_batch(batch)
            except Exception:  # noqa: BLE001 —— 一批失败不能拖垮引擎
                logger.exception("处理信号批失败（%d 笔）", len(batch))
            finally:
                for _ in batch:
                    self._queue.task_done()
            if stop_after:
                self._queue.task_done()  # 对应 _STOP 那次 get
                return

    def _collect_window(self, batch: list) -> bool:
        """聚合窗口内继续攒信号；遇到 _STOP 返回 True（处理完当前批后退出）。

        idle_flush_s > 0 时：窗口内静默超过该时长就提前收批——碎片建仓的
        连发间隔是毫秒级，静默半秒基本等于不会再来了，没必要把单笔信号
        也压满整个窗口（那是白给的延迟）。
        """
        window = self.config.aggregate.window_s
        if window <= 0:
            return False
        idle = self.config.aggregate.idle_flush_s
        deadline = time.monotonic() + window
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            timeout = min(remaining, idle) if idle > 0 else remaining
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                return False
            if item is _STOP:
                return True
            batch.append(item)

    def _process(self, signal: Signal) -> None:
        """处理单笔信号（等价于长度为 1 的批；测试与旧路径入口）。"""
        self._process_batch([signal])

    def _process_batch(self, batch: list[Signal]) -> None:
        pending = self._prepare(batch)
        if not pending:
            return
        planned: list[_Planned] = []
        groups: dict[tuple, list[PendingSignal]] = {}
        for item in pending:
            groups.setdefault(group_key(item.signal), []).append(item)
        for members in groups.values():
            group = merge_pending(members)
            try:
                item = self._plan_group(group)
            except Exception:  # noqa: BLE001
                logger.exception("信号组仓位计算失败: %s", self._label(group))
                self._mark_group(group, "error", "仓位计算异常，详见日志")
                continue
            if item is not None:
                planned.append(item)

        for item in self._net_planned(planned):
            try:
                self._execute_planned(item)
            except Exception:  # noqa: BLE001
                logger.exception("执行失败: %s", self._label(item.group))
                self._mark_group(item.group, "error", "执行异常，详见日志")

    def _prepare(self, batch: list[Signal]) -> list[PendingSignal]:
        """逐笔落库、更新镜像、过基础过滤；返回待并单的信号。"""
        out: list[PendingSignal] = []
        for signal in batch:
            trade = signal.trade
            signal_id, fresh = self._ledger.record_signal(signal)
            if not fresh or signal_id is None:
                logger.debug("信号已处理过，跳过: %s", trade.key)
                continue
            # 无论这笔最终跟不跟，目标的持仓镜像都要如实更新
            prev = self._mirror.apply_trade(trade)
            # 已持仓时目标卖出，「他还在场」的前提已没了：离场信号绕过进场闸
            # （SELL 是少数，逐笔查一次 sqlite 可接受）
            holding = (
                trade.side == "SELL"
                and self._ledger.position_size(trade.asset) > 1e-9
            )
            ok, reason = self._filter.check(signal, holding=holding)
            if not ok:
                self._ledger.update_signal(signal_id, "filtered", reason)
                logger.info("过滤: %s —— %s", reason, self._trade_label(trade))
                continue
            out.append(PendingSignal(signal_id=signal_id, signal=signal, prev_target_size=prev))
        return out

    def _plan_group(self, group: MergedGroup) -> _Planned | None:
        """市场元数据 + 按市场期限复核时效 + 金额过滤 + 仓位计算。"""
        label = self._label(group)
        try:
            market = self._clob.get_market(group.trade.condition_id)
        except ClobError as exc:
            self._mark_group(group, "error", f"拉取市场元数据失败: {exc}")
            logger.warning("拉取市场元数据失败，放弃该信号组: %s —— %s", exc, label)
            return None

        if group.trade.side == "BUY":
            horizon = self._resolution_horizon_reason(market)
            if horizon:  # 短周期市场对 BUY 关闸（组内同市场，一荣俱荣）
                self._mark_group(group, "filtered", horizon)
                return None

        narrowed = self._enforce_market_age(group, market)
        if narrowed is None:
            return None
        group = narrowed
        trade = group.trade
        label = self._label(group)

        ok, reason = self._filter.check_notional(trade.notional, group.count)
        if not ok:
            self._mark_group(group, "filtered", reason)
            return None

        merged_signal = Signal(
            trade=trade, target=group.target, received_at=group.earliest_received
        )
        if trade.side == "BUY":
            book = None
            if self.config.sizing.depth_aware:
                try:
                    book = self._clob.get_book(trade.asset)
                except ClobError as exc:
                    logger.debug("深度封顶取订单簿失败，退回按 ratio 定量: %s", exc)
            intent, reason = plan_buy(
                merged_signal, market, self.config.sizing, self.config.execution, book=book
            )
            if intent is not None and group.count > 1:
                merge_note = f"并单 {group.count} 笔"
                note = f"{intent.note}，{merge_note}" if intent.note else merge_note
                intent = dataclasses.replace(intent, note=note)
        else:
            intent, reason = self._plan_sell(merged_signal, market, group.prev_target_size)
            if intent is not None and group.count > 1:
                intent = dataclasses.replace(intent, note=f"{intent.note}，并单 {group.count} 笔")
        if intent is None:
            self._mark_group(group, "skipped", reason)
            return None
        return _Planned(group=group, intent=intent, market=market)

    def _resolution_horizon_reason(self, market: MarketInfo) -> str:
        """BUY 期限闸：距结算不足下限天数则返回拦截理由，否则空串（放行）。

        只作用于开新仓（调用方已判定 side==BUY）。end_ts 缺失按短周期保守处理，
        文案与正常不过闸区分开。
        """
        min_days = self.config.filters.min_days_to_resolution
        if not min_days:  # None / 0 → 闸关闭
            return ""
        end_ts = getattr(market, "end_ts", 0.0) or 0.0
        if not end_ts:
            return "市场缺少结束时间元数据，按短周期保守不开新仓"
        days_left = (end_ts - time.time()) / 86400
        if days_left < min_days:
            return f"距结算 {days_left:.1f} 天 < 下限 {min_days:.1f} 天，短周期市场不开新仓"
        return ""

    def _enforce_market_age(self, group: MergedGroup, market: MarketInfo) -> MergedGroup | None:
        """按该市场的精确时效上限复核信号组（逐笔粗筛只按放宽上限放行）。

        长线市场放宽、短线维持严格（见 SignalFilter.age_limit_for）。
        超龄成员标 filtered，剩余成员重新并组；全部超龄返回 None。
        """
        # 已持仓的离场组：目标离场即信号，整组跳过精筛（晚离场胜过拿到归零）
        if (
            group.trade.side == "SELL"
            and self._ledger.position_size(group.trade.asset) > 1e-9
        ):
            return group
        limit = self._filter.age_limit_for(market)
        now = time.time()
        fresh: list[PendingSignal] = []
        for member in group.members:
            age = now - member.signal.trade.timestamp
            if age > limit:
                self._ledger.update_signal(
                    member.signal_id, "filtered",
                    f"信号已过期 {age:.0f}s（该市场时效上限 {limit:.0f}s）",
                )
            else:
                fresh.append(member)
        if not fresh:
            logger.info(
                "过滤: 信号组全部超过该市场时效上限 %.0fs —— %s", limit, self._label(group)
            )
            return None
        if len(fresh) == len(group.members):
            return group
        return merge_pending(fresh)

    def _net_planned(self, planned: list[_Planned]) -> list[_Planned]:
        """多目标轧差：同一 token 在本批内同时有买有卖时，只执行净头寸。"""
        if not self.config.aggregate.net_across_targets or len(planned) < 2:
            return planned
        by_token: dict[str, list[_Planned]] = {}
        for item in planned:
            by_token.setdefault(item.intent.token_id, []).append(item)
        out: list[_Planned] = []
        for items in by_token.values():
            buys = [p for p in items if p.intent.side == "BUY"]
            sells = [p for p in items if p.intent.side == "SELL"]
            if not buys or not sells:
                out.extend(items)
                continue
            sum_buy = sum(p.intent.size for p in buys)
            sum_sell = sum(p.intent.size for p in sells)
            net = sum_buy - sum_sell
            dominant, offset = (buys, sells) if net >= 0 else (sells, buys)
            base = f"多目标轧差：窗口内买 {sum_buy:.2f} / 卖 {sum_sell:.2f} 份对冲"
            for p in offset:
                self._mark_group(p.group, "netted", base)
            remaining = abs(net)
            min_size = items[0].market.min_size
            if remaining < min_size:
                for p in dominant:
                    self._mark_group(
                        p.group, "netted",
                        f"{base}，净额 {remaining:.2f} 份低于最小下单量 {min_size:.2f}",
                    )
                continue
            factor = remaining / sum(p.intent.size for p in dominant)
            for p in dominant:
                if factor >= 1.0 - 1e-9:
                    out.append(p)
                    continue
                new_size = floor_to(p.intent.size * factor, SIZE_STEP)
                if new_size < min_size:
                    self._mark_group(
                        p.group, "netted",
                        f"{base}，缩量后 {new_size:.2f} 份低于最小下单量 {min_size:.2f}",
                    )
                    continue
                note = p.intent.note
                note = f"{note}，轧差缩量" if note else "轧差缩量"
                out.append(dataclasses.replace(
                    p, intent=dataclasses.replace(p.intent, size=new_size, note=note),
                ))
        return out

    def _execute_planned(self, item: _Planned) -> None:
        group, intent = item.group, item.intent
        # 目标地址从信号组传入，供风控的单目标单市场敞口闸归因
        ok, reason = self._risk.check(intent, item.market, group.target.address)
        if not ok:
            self._mark_group(group, "risk_blocked", reason)
            self._notifier.send(f"⛔ 风控拦截：{reason} —— {self._label(group)}")
            return

        result: ExecutionResult = self._executor.execute(intent)
        retry_s = self.config.execution.retry_no_fill_s
        if result.status == "rejected" and retry_s:
            # 盘口是流动的：限价内暂时没对手盘，隔几秒常会回来；只重试这一种干净失败
            logger.info("限价内无对手盘，%.1fs 后重试一次 —— %s", retry_s, self._label(group))
            time.sleep(retry_s)
            second = self._executor.execute(intent)
            if second.status == "rejected":
                result = dataclasses.replace(second, detail=f"{second.detail}（已重试 1 次）")
            else:
                result = dataclasses.replace(
                    second, detail=second.detail or "首次限价内无对手盘，重试 1 次后成交"
                )
        self._ledger.record_order(
            group.signal_ids[0], intent,
            mode=self._executor.mode, status=result.status,
            filled_size=result.filled_size, avg_price=result.avg_price,
            slippage=result.slippage, detail=result.detail,
            apply_fill=getattr(self._executor, "applies_fills", True),
        )
        status = "executed" if result.ok else "no_fill"
        detail = result.detail
        if group.count > 1:
            suffix = f"并单 {group.count} 笔 → 订单挂在信号 #{group.signal_ids[0]}"
            detail = f"{detail}；{suffix}" if detail else suffix
        for signal_id in group.signal_ids:
            self._ledger.update_signal(signal_id, status, detail)
        self._notifier.send(self._describe(intent, result, group))

    def _plan_sell(
        self, signal: Signal, market: MarketInfo, prev_target_size: float
    ) -> tuple[OrderIntent | None, str]:
        """卖出跟随：目标卖掉他持仓的 x%，我们也卖自己持仓的 x%。"""
        trade = signal.trade
        own = self._ledger.position_size(trade.asset)
        if own <= 0:
            return None, "自己没有该市场持仓，忽略卖出信号"
        if prev_target_size > 0:
            fraction = min(1.0, trade.size / prev_target_size)
        else:
            # 镜像里没有记录（对方启动前建的仓且对账还没跑到）——按全平跟随，保守离场
            fraction = 1.0
            logger.info(
                "目标 %s 卖出了镜像未记录的持仓，按全平跟随",
                _short(trade.proxy_wallet),
            )
        sell_size = floor_to(own * fraction, SIZE_STEP)
        if own - sell_size < market.min_size:
            sell_size = own  # 余量将低于最小下单量成为死仓，干脆全平
        if sell_size < market.min_size:
            return None, (
                f"计划卖出 {sell_size:.2f} 份低于市场最小下单量 "
                f"{market.min_size:.2f}（持仓 {own:.2f}）"
            )
        limit = sell_limit_price(trade.price, market, self.config.execution)
        return (
            OrderIntent(
                token_id=trade.asset,
                condition_id=trade.condition_id,
                side="SELL",
                limit_price=limit,
                size=round(sell_size, 2),
                ref_price=trade.price,
                neg_risk=market.neg_risk,
                tick_size=market.tick_size,
                title=trade.title,
                outcome=trade.outcome,
                note=f"跟随卖出 {fraction:.0%}",
            ),
            "",
        )

    # ---- 对账 ----

    def _reconcile_loop(self) -> None:
        while not self._reconcile_stop.wait(self.config.reconcile_interval_s):
            try:
                self.reconcile_once()
            except Exception:  # noqa: BLE001 —— 对账失败下轮再试
                logger.exception("对账失败，下一轮继续")
            try:
                self._maybe_check_health()
            except Exception:  # noqa: BLE001 —— 巡检失败不影响对账主流程
                logger.exception("目标健康巡检失败，下一轮继续")

    def reconcile_once(self) -> None:
        """刷新目标持仓镜像；纸面自动结算已出结果的持仓；实盘同步持仓并提醒可赎回。"""
        failed_refresh: set[str] = set()  # 本轮镜像拉取失败的地址（强制离场据此避让陈旧镜像）
        if self._data is not None:
            for address in self._targets:
                try:
                    positions = self._data.get_positions(address)
                except DataApiError as exc:
                    logger.warning("拉取 %s 持仓失败，镜像维持现状: %s", _short(address), exc)
                    failed_refresh.add(address)
                    continue
                self._mirror.replace(address, {p.asset: p.size for p in positions})
                logger.debug("镜像已刷新: %s 持有 %d 个仓位", _short(address), len(positions))
                # 顺手回填账本里缺失的元数据（成交推送偶尔缺 title/conditionId）
                meta = {
                    p.asset: (p.title, p.condition_id)
                    for p in positions
                    if p.title or p.condition_id
                }
                if meta:
                    self._ledger.backfill_position_meta(meta)

        if self.config.mode != "live" and self.config.auto_settle_resolved:
            self._settle_resolved_positions()

        # 兜底：建这笔仓的目标全清仓了，我们的卖出信号却漏了 → 强平自仓（两轮确认）
        self._maybe_force_exit(failed_refresh)

        if self.config.mode != "live" or not self._own_address or self._data is None:
            return
        try:
            own_positions = self._data.get_positions(self._own_address)
        except DataApiError as exc:
            logger.warning("拉取自己持仓失败，账本维持现状: %s", exc)
            return
        self._ledger.sync_positions(own_positions)
        logger.info("实盘持仓已对账（%d 个仓位）", len(own_positions))
        redeemable = [
            p for p in own_positions
            if p.redeemable and p.size > 0 and p.asset not in self._redeem_notified
        ]
        for p in redeemable:
            self._redeem_notified.add(p.asset)
            self._notifier.send(
                f"🎉 市场已结算可赎回：{p.size:.2f} 份 [{p.outcome}] {p.title} —— "
                "请在 Polymarket 页面 redeem 换回 USDC"
            )

    def _maybe_force_exit(self, failed_refresh: set[str]) -> None:
        """对账兜底：建仓目标全部清仓时强平自仓，防卖出信号漏了拿到归零结算。

        判定：自己每一笔持仓，取「建仓者」（下过 BUY 且成交的目标）与当前 targets
        的交集；交集为空不碰（不是我们跟出来的，或建仓者已被用户删除，不越权）。
        所有建仓者的镜像持仓都 ≤ 1 份（份额级 dust，目标常留渣）才算全清仓。任一
        建仓者本轮镜像拉取失败 → 跳过该 token（绝不拿陈旧镜像误杀）。paused 的目标
        照样参与判定——兜底保护的是我们的钱，与暂停语义无关。

        两轮确认：首轮命中只登记进 _pending_force_exit，下一轮仍命中才真正执行；
        条件消失即移出。防 data-api 瞬时返回空持仓造成误杀。
        """
        if not self.config.force_exit_on_target_flat or self._data is None:
            return
        targets = set(self._targets)
        candidates: list[tuple[PositionRow, set[str]]] = []
        for position in self._ledger.positions():  # size > 0
            token = position.token_id
            builders = self._ledger.buy_builders(token) & targets
            if not builders:
                continue  # 不是我们跟出来的仓，或建仓者已被移出配置：不越权
            if builders & failed_refresh:
                continue  # 某建仓者镜像本轮没刷新，宁可下轮再判也不误杀
            if all(self._mirror.size_of(b, token) <= 1.0 for b in builders):
                candidates.append((position, builders))
        live_tokens = {p.token_id for p, _ in candidates}
        self._pending_force_exit &= live_tokens  # 条件消失的移出待确认集合
        for position, builders in candidates:
            token = position.token_id
            if token not in self._pending_force_exit:
                self._pending_force_exit.add(token)  # 首轮命中：只登记，下轮确认
                logger.info(
                    "强制离场候选（首轮登记，下轮确认）: [%s] %s",
                    position.outcome, position.title or _short(token),
                )
                continue
            if self._force_exit_position(position, builders):
                self._pending_force_exit.discard(token)

    def _force_exit_position(self, position: PositionRow, builders: set[str]) -> bool:
        """全仓 FAK 平掉一笔持仓；返回 True=已了结（成交/已结算跳过），False=盘口缺失下轮再试。

        复用现有执行/记账路径：订单挂 signal_id=0（与 REDEEM 同惯例），限价按最优买价
        减滑点上限取到 tick。同时记 force_exit 事件并通知。
        """
        token = position.token_id
        try:
            market = self._clob.get_market(position.condition_id, fresh=True)
        except ClobError as exc:
            logger.warning("强制离场拉取市场失败，下轮再试 [%s]: %s", _short(token), exc)
            return False
        if market.resolved:
            return True  # 已结算：交给结算/赎回路径，安静跳过
        try:
            book = self._clob.get_book(token)
        except ClobError as exc:
            logger.warning("强制离场拉取订单簿失败，下轮再试 [%s]: %s", _short(token), exc)
            return False
        if not book.bids:
            logger.info("强制离场：[%s] 盘口无买单，下轮再试", position.title or _short(token))
            return False
        best_bid = book.bids[0].price
        intent = OrderIntent(
            token_id=token,
            condition_id=position.condition_id,
            side="SELL",
            limit_price=sell_limit_price(best_bid, market, self.config.execution),
            size=round(position.size, 2),
            ref_price=best_bid,
            neg_risk=market.neg_risk,
            tick_size=market.tick_size,
            title=position.title,
            outcome=position.outcome,
            note="对账离场：建仓目标已清仓",
        )
        result: ExecutionResult = self._executor.execute(intent)
        self._ledger.record_order(
            0, intent, mode=self._executor.mode, status=result.status,
            filled_size=result.filled_size, avg_price=result.avg_price,
            slippage=result.slippage,
            # 订单行自带「对账离场」标注：复盘最近订单时能一眼与常规跟卖区分
            detail=f"{intent.note}；{result.detail}" if result.detail else intent.note,
            apply_fill=getattr(self._executor, "applies_fills", True),
        )
        detail = (
            f"建仓目标 {'、'.join(_short(b) for b in sorted(builders))} 已全部清仓，"
            f"全仓强制离场 {intent.size:.2f} 份 @≤{intent.limit_price:.3f}"
        )
        self._ledger.record_event("force_exit", ",".join(sorted(builders)), detail)
        prefix = "📝 纸面" if self._executor.mode == "paper" else "💰 实盘"
        self._notifier.send(
            f"🚪 {prefix}对账离场：{detail} —— [{position.outcome}] {position.title}"
        )
        return True

    def _maybe_check_health(self) -> None:
        interval = self.config.health.check_interval_s
        if interval <= 0 or self._data is None:
            return
        now = time.time()
        if now - self._last_health_check < interval:
            return
        self._last_health_check = now
        self._ledger.set_state("health_last_check_ts", str(now))
        self.check_targets_health()

    def check_targets_health(self) -> None:
        """试用期考核：用 scout 的排除规则复查在跟目标，变质即暂停、恢复即复跟。

        手动暂停（配置 paused=true 且非巡检所为）的目标不碰；数据拉取失败
        跳过该目标（绝不因网络抖动误停）。
        """
        if self._data is None:
            return
        from ..scout import ScoutConfig, replay
        from ..scout.score import evaluate_health

        scout_config = ScoutConfig()
        for index, (address, target) in enumerate(self._targets.items()):
            if target.paused and address not in self._health_paused:
                continue  # 手动暂停的目标，巡检不越权
            if index and scout_config.request_delay_s > 0:
                time.sleep(scout_config.request_delay_s)
            try:
                tape = self._data.get_trades(address, limit=500)
                positions = self._data.get_positions(address)
            except DataApiError as exc:
                logger.warning("健康巡检拉取 %s 失败，本轮跳过: %s", _short(address), exc)
                continue
            stats = replay(address, tape, quick_window_s=scout_config.quick_window_s)
            # 考核版口径：活仓浮亏 + 窗口净盈亏（老死仓不追溯，见 evaluate_health）
            verdict = evaluate_health(stats, positions, tape, scout_config)

            if verdict.eligible:
                if address in self._health_paused and self.config.health.auto_resume:
                    target.paused = False
                    self._health_paused.discard(address)
                    self._persist_health_paused()
                    self._ledger.record_event("health_resume", address, "恢复合格，自动复跟")
                    self._notifier.send(
                        f"✅ 健康巡检：{_short(address)} 已恢复合格，自动复跟"
                    )
                else:
                    logger.debug("健康巡检 ✓ %s", _short(address))
                continue

            reasons = "；".join(verdict.reasons)
            if address in self._health_paused:
                logger.info("健康巡检：%s 仍不合格（%s），维持暂停", _short(address), reasons)
            elif self.config.health.auto_pause:
                target.paused = True
                self._health_paused.add(address)
                self._persist_health_paused()
                self._ledger.record_event("health_pause", address, reasons)
                self._notifier.send(
                    f"⚠️ 健康巡检：{_short(address)} 命中排除规则（{reasons}），"
                    "已自动暂停跟单（镜像继续维护，恢复合格自动复跟）"
                )
            else:
                self._notifier.send(
                    f"⚠️ 健康巡检：{_short(address)} 命中排除规则（{reasons}），"
                    "建议人工复查（auto_pause 已关闭，仍在跟单）"
                )

    # ---- 候选发现（票池 + 涓流评估）----

    def observe_site_trade(self, trade: Trade) -> None:
        """stream 线程回调：把全站每一笔成交累进内存增量（发现线程稍后批量落库）。

        零 API 成本的被动累积——全站峰值几十笔/秒，dict 累加没有压力；绝不逐笔写库。
        发现关闭时直接丢弃（不累积，避免无人落库时内存无界增长）。
        """
        if self.config.health.discover_interval_s <= 0 or self._data is None:
            return
        wallet = trade.proxy_wallet
        if not wallet:
            return
        notional = trade.notional
        with self._universe_lock:
            slot = self._universe_accum.get(wallet)
            if slot is None:
                self._universe_accum[wallet] = {
                    "notional": notional, "trades": 1, "last_seen": trade.timestamp,
                }
            else:
                slot["notional"] += notional
                slot["trades"] += 1
                slot["last_seen"] = max(slot["last_seen"], trade.timestamp)

    def _discover_loop(self) -> None:
        # 每分钟醒来涓流一小口（墙钟 + 账本持久化计时），重启不把周期计时清零
        while not self._reconcile_stop.wait(60):
            try:
                self._discover_tick()
            except Exception:  # noqa: BLE001 —— 发现失败不影响任何主流程
                logger.exception("候选发现失败，下一轮继续")

    def _discover_tick(self) -> None:
        """一次发现节拍：（冷启动/到点）刷新票池 → 涓流评估 → 每小时摘要+招募。"""
        if self.config.health.discover_interval_s <= 0 or self._data is None:
            return
        now = time.time()
        if self._ledger.discover_universe_count() == 0:
            # 冷启动：全新账本票池为空 → 立刻灌一批，不等周期
            self._bootstrap_universe()
            self._last_discover = now
            self._ledger.set_state("discover_last_run_ts", str(now))
        elif now - self._last_discover >= self.config.health.discover_interval_s:
            # 到点：票池刷新（stream 增量落库 + 排行榜并轨 + 修剪）
            self._refresh_universe()
            self._last_discover = now
            self._ledger.set_state("discover_last_run_ts", str(now))
        if now - self._last_universe_flush >= 600:
            # stream 增量按 ~10 分钟节拍独立落库：不等每日刷新，新活跃地址十分钟内
            # 即进入涓流评估的候选范围；重启最多丢十分钟的内存增量
            self._flush_universe_accumulator()
            self._last_universe_flush = now
        self._trickle_evaluate()
        self._maybe_discover_summary(now)

    def _flush_universe_accumulator(self) -> int:
        """把内存增量批量 upsert 进票池表并清空（发现线程调用）。

        先在锁内换出整批、立刻释放锁，DB 写在锁外做——不拿累积器锁阻塞 stream 线程。
        """
        with self._universe_lock:
            if not self._universe_accum:
                return 0
            batch = self._universe_accum
            self._universe_accum = {}
        rows = [
            {"wallet": w, "notional": v["notional"], "trades": v["trades"], "source": "stream"}
            for w, v in batch.items()
        ]
        return self._ledger.discover_universe_upsert(rows)

    def _leaderboard_fetch(self, window: str, rank_type: str) -> list[str]:
        """拉一种排行榜组合，limit 逐档降级；全失败返回空（跳过该组合）。

        接口 limit 硬上限 50（2026-07 实测超出返回 400），首档直接 50；
        保留降级阶梯纯为防御——接口再改上限时自动适应而不是整源熄火。
        """
        from ..scout.runner import ScoutError, candidates_from_leaderboard

        for limit in (50, 25):
            try:
                return candidates_from_leaderboard(
                    window=window, rank_type=rank_type, limit=limit
                )
            except ScoutError as exc:
                logger.info(
                    "排行榜 %s/%s limit=%d 拉取失败，降级重试: %s",
                    window, rank_type, limit, exc,
                )
        logger.warning("排行榜 %s/%s 各档 limit 全失败，跳过该组合", window, rank_type)
        return []

    def _leaderboard_join(self) -> int:
        """排行榜并轨：7d/30d × pnl/vol 四组合并入票池。

        这些行没有逐地址成交额（notional 传 0、只保证在池、刷新 last_seen）；
        某组合彻底拉不到就跳过它，全挂则本源不贡献。返回并入的地址数。
        """
        wallets: set[str] = set()
        for window in ("7d", "30d"):
            for rank_type in ("pnl", "vol"):
                wallets.update(self._leaderboard_fetch(window, rank_type))
        if not wallets:
            logger.info("排行榜并轨：本轮没拿到任何候选（源不可用，已跳过）")
            return 0
        return self._ledger.discover_universe_upsert(
            [{"wallet": w, "notional": 0.0, "trades": 0, "source": "leaderboard"} for w in wallets]
        )

    def _bootstrap_universe(self) -> None:
        """冷启动填充：现有 candidates_from_recent_trades 全站快照 + 排行榜并轨，立刻灌池。"""
        from ..scout.runner import candidates_from_recent_trades

        n = self.config.health.discover_candidates
        try:
            snapshot = candidates_from_recent_trades(self._data, top=n)
        except DataApiError as exc:
            logger.warning("冷启动全站快照拉取失败: %s", exc)
            snapshot = []
        if snapshot:
            # 快照来源没有逐地址成交额（notional=0，真实成交额由 stream 增量补齐）；
            # candidates_from_recent_trades 已按成交额降序，按序插入让 rowid 保留排名。
            self._ledger.discover_universe_upsert(
                [{"wallet": w, "notional": 0.0, "trades": 0, "source": "snapshot"} for w in snapshot]
            )
        lb = self._leaderboard_join()
        logger.info("候选发现冷启动：快照 %d 个 + 排行榜 %d 个，票池已初始化", len(snapshot), lb)

    def _refresh_universe(self) -> None:
        """到点的票池刷新：stream 增量落库 + 排行榜并轨 + 修剪（时间 + 容量）。"""
        flushed = self._flush_universe_accumulator()
        lb = self._leaderboard_join()
        pruned = self._ledger.discover_universe_prune(
            max_age_s=7 * 86400,
            max_rows=self.config.health.discover_universe_size * 4,
        )
        logger.info("票池刷新：stream 增量落库 %d、排行榜并轨 %d、修剪 %d", flushed, lb, pruned)

    def _trickle_evaluate(self) -> int:
        """每次醒来评估一小口：按优先级从票池取地址走 scout 完整评估，结论写回票池行。

        速率 = discover_eval_per_min（float，≤0 关闭）；用小数预算累加器支持 <1/min 的
        速率。候选优先级（未评估 > 冷却到期）、冷却、剔除在跟/黑名单全在 Ledger 里判。
        """
        per = self.config.health.discover_eval_per_min
        if per <= 0:
            return 0
        self._eval_budget += per
        n = int(self._eval_budget)
        if n <= 0:
            return 0
        self._eval_budget -= n

        from ..scout import ScoutConfig, scout_addresses

        exclude = set(self._targets) | set(self.config.health.recruit_blocklist)
        due = self._ledger.discover_universe_due(
            limit=n,
            universe_size=self.config.health.discover_universe_size,
            reeval_after_s=self.config.health.discover_reeval_days * 86400,
            exclude=exclude,
        )
        if not due:
            return 0
        verdicts = scout_addresses(self._data, due, config=ScoutConfig())
        now = time.time()
        for v in verdicts:
            self._ledger.discover_universe_record_eval(
                v.address, score=v.score, eligible=v.eligible,
                verdict=v.to_dict(), now=now,
            )
            if v.eligible and v.address not in self._targets:
                self._discover_new_eligible[v.address] = v  # 攒到每小时摘要点
        return len(due)

    def _maybe_discover_summary(self, now: float) -> None:
        """每小时最多一条摘要 + 从名录择优招募；滚动名录随之刷新。

        通知和招募的取数源是两回事，故此处解耦：通知播报「这小时新发现了谁」
        （当小时新合格者），招募则从全量名录挑最好的。只看当小时新面孔会漏掉
        上小时评出的高分候选、却招走这小时碰巧评出的擦线者——名录里躺着历次
        评估攒下的全部合格者，按分数择优才是保守口径。
        """
        if now - self._last_discover_notify < 3600:
            return
        self._last_discover_notify = now
        new = sorted(self._discover_new_eligible.values(), key=lambda v: -v.score)
        self._discover_new_eligible.clear()
        # 名录已按 last_score 降序并排除在跟目标；转成轻量候选对象喂给 _recruit
        roster = self._ledger.discover_universe_roster(exclude=set(self._targets))
        recruited = self._recruit([_RosterCandidate.from_entry(e) for e in roster])
        self._write_discover_roster(now)        # 再写名录：自动排除刚招募进池的地址
        fresh = self._dedup_notify(new, now)    # 同一地址 7 天内不重复上榜通知
        if fresh or recruited:
            self._notifier.send(self._discover_summary_text(fresh, recruited))

    def _dedup_notify(self, verdicts, now: float) -> list:
        """7 天去重：返回本轮该上榜通知的新合格者，并把它们记进已通知集合（持久化到 state）。

        读写时顺手清掉 7 天前的过期项，集合不会无限膨胀。
        """
        raw = self._ledger.get_state("discover_notified")
        try:
            notified = json.loads(raw) if raw else {}
        except ValueError:
            notified = {}
        if not isinstance(notified, dict):
            notified = {}
        cutoff = now - 7 * 86400
        notified = {
            a: ts for a, ts in notified.items()
            if isinstance(ts, (int, float)) and ts >= cutoff
        }
        fresh = []
        for v in verdicts:
            if v.address in notified:
                continue
            notified[v.address] = now
            fresh.append(v)
        self._ledger.set_state("discover_notified", json.dumps(notified))
        return fresh

    def _discover_summary_text(self, fresh, recruited) -> str:
        # 招募源解耦后，「这小时没有新面孔、但从名录招到了人」是常态，
        # 此时不摆一个空的「Top：」清单头
        lines = (
            [f"🔭 候选发现摘要：新合格 {len(fresh)} 个面孔（票池涓流评估，均不在跟单名单）。Top："]
            if fresh
            else ["🔭 候选发现摘要：本小时无新合格面孔（名录存量见下）。"]
        )
        for v in fresh[:5]:
            s = v.stats
            if s:
                win = f"{s.win_rate:.0%}({s.matched_sells})" if s.win_rate is not None else "未知"
                lines.append(
                    f"  {_short(v.address)} 分{v.score:.0f} 盈亏${s.realized_pnl:+,.0f} "
                    f"胜率{win} 笔均${s.avg_trade_usdc:,.0f}"
                )
            else:
                lines.append(f"  {_short(v.address)} 分{v.score:.0f}")
        if recruited:
            health = self.config.health
            lines.append(
                f"🤝 已自动加入纸面跟单 {len(recruited)} 个："
                + "、".join(_short(a) for a in recruited)
                + f"（ratio {health.recruit_ratio} / 单笔 ${health.recruit_max_per_trade_usdc:.0f}，"
                "变质由健康巡检自动暂停）"
            )
        elif fresh:
            out_path = Path(self.config.ledger_path).parent / "discover-latest.json"
            lines.append(f"完整名单 {out_path}；要跟谁，编辑配置 targets 后重启")
        return "\n".join(lines)

    def _write_discover_roster(self, now: float) -> None:
        """滚动名录 discover-latest.json：票池里当前合格且不在跟的全部地址（按分数降序）。

        原子写（tmp + replace）。eligible 每项是最近一次评估的 Verdict.to_dict() + evaluated_at。
        """
        out_path = Path(self.config.ledger_path).parent / "discover-latest.json"
        eligible = self._ledger.discover_universe_roster(exclude=set(self._targets))
        payload = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "universe_size": self._ledger.discover_universe_count(),
            "evaluated_24h": self._ledger.discover_evaluated_since(now - 86400),
            "eligible": eligible,
        }
        tmp = out_path.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out_path)

    def _recruit(self, eligible) -> list[str]:
        """把合格候选自动加入跟单（仅纸面模式），并持久化到招募档案。

        eligible 是按分数降序的候选序列，元素只需有 .address / .score
        （Verdict 或 _RosterCandidate 都行）。watcher/stream 通过 on_new_target
        回调开始盯新地址（老成交走基线机制不会刷成新信号）；镜像由下一轮对账补快照。
        """
        health = self.config.health
        if not eligible or not health.auto_recruit:
            return []
        if self.config.mode != "paper":
            logger.warning("auto_recruit 仅纸面模式生效，实盘模式忽略（%d 个合格候选）", len(eligible))
            return []
        blocked = set(health.recruit_blocklist)
        recruited: list[str] = []
        for verdict in eligible:  # 已按分数降序
            if len(self._targets) >= health.recruit_max_targets:
                logger.info("目标总数已达上限 %d，本轮不再招募", health.recruit_max_targets)
                break
            # 每轮限量：名额一次性灌满就没法分批看表现了，剩下的留到下一轮
            if 0 < health.recruit_max_per_round <= len(recruited):
                logger.info("本轮已招满 %d 个，其余候选留到下一轮", health.recruit_max_per_round)
                break
            if verdict.address in blocked:
                logger.info("候选 %s 在黑名单，跳过招募", _short(verdict.address))
                continue
            # 分数门槛：合格只是及格线，擦线者不值得占一个名额
            if verdict.score < health.recruit_min_score:
                logger.debug(
                    "候选 %s 分%.1f 低于招募门槛 %.1f，跳过",
                    _short(verdict.address), verdict.score, health.recruit_min_score,
                )
                continue
            target = TargetConfig(
                address=verdict.address,
                ratio=health.recruit_ratio,
                max_per_trade_usdc=health.recruit_max_per_trade_usdc,
            )
            self._targets[target.address] = target
            self._recruited[target.address] = {
                "address": target.address,
                "ratio": health.recruit_ratio,
                "max_per_trade_usdc": health.recruit_max_per_trade_usdc,
                "recruited_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "score": verdict.score,
            }
            recruited.append(target.address)
            self._ledger.record_event(
                "recruit", target.address,
                f"分{verdict.score:.0f}，ratio {health.recruit_ratio}/单笔 ${health.recruit_max_per_trade_usdc:.0f}",
            )
            if self._on_new_target is not None:
                try:
                    self._on_new_target(target.address)
                except Exception:  # noqa: BLE001 —— 回调失败不阻塞招募（轮询兜底仍会盯）
                    logger.exception("通知监控层新目标失败: %s", target.address)
        if recruited:
            path = _recruited_path(self.config)
            tmp = path.with_suffix(".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(list(self._recruited.values()), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
            logger.info("已招募 %d 个新目标并写入档案: %s", len(recruited), path)
        return recruited

    def _settle_resolved_positions(self) -> None:
        """纸面自动结算：市场关闭且有 winner 后，按 1.00/0.00 把持仓入账清仓。

        实盘不走这里——真实 redeem 是链上交易（需要 gas），仍由上面的
        可赎回提醒人工处理。
        """
        for position in self._ledger.positions():
            try:
                market = self._clob.get_market(position.condition_id, fresh=True)
            except ClobError as exc:
                logger.debug("结算检查拉取市场失败 %s: %s", position.condition_id, exc)
                continue
            if not market.resolved:
                continue
            price = 1.0 if position.token_id in market.winner_token_ids else 0.0
            realized = self._ledger.settle_position(
                position.token_id, price, mode=self._executor.mode
            )
            if realized is None:
                continue
            word = "赢" if price > 0 else "输"
            self._notifier.send(
                f"🧾 市场已结算（{word}）：{position.size:.2f} 份 [{position.outcome}] "
                f"{position.title} 按 {price:.2f} 入账，已实现 {realized:+.2f}"
            )

    # ---- 展示 ----

    def _trade_label(self, trade: Trade) -> str:
        return (
            f"{_short(trade.proxy_wallet)} {trade.side} {trade.size:.2f} @ {trade.price:.3f} "
            f"[{trade.outcome}] {trade.title}"
        )

    def _label(self, group: MergedGroup) -> str:
        label = self._trade_label(group.trade)
        if group.count > 1:
            label += f"（并 {group.count} 笔）"
        return label

    def _mark_group(self, group: MergedGroup, status: str, detail: str) -> None:
        for signal_id in group.signal_ids:
            self._ledger.update_signal(signal_id, status, detail)
        logger.info("%s: %s —— %s", status, detail, self._label(group))

    def _describe(self, intent: OrderIntent, result: ExecutionResult, group: MergedGroup) -> str:
        prefix = "📝 纸面" if self._executor.mode == "paper" else "💰 实盘"
        head = (
            f"{prefix} {intent.side} {intent.size:.2f} 份 @≤{intent.limit_price:.3f} "
            f"[{intent.outcome}] {intent.title}"
        )
        if result.status in ("filled", "partial"):
            body = (
                f"→ 成交 {result.filled_size:.2f} @ {result.avg_price:.3f}"
                f"（${result.notional:.2f}，滑点 {result.slippage:+.3f}）"
            )
            if result.status == "partial":
                body += f"；{result.detail}"
        elif result.status == "submitted":
            body = f"→ 已提交 {result.detail}"
        else:
            body = f"→ 未成交：{result.detail}"
        note = f"（{intent.note}）" if intent.note else ""
        return f"{head} {body}{note} 跟随 {_short(group.trade.proxy_wallet)}"
