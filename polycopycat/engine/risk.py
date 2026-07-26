"""风控闸门：意图在执行前的最后一道检查。

原则：宁可漏跟，不可爆仓。卖出（减仓）只做基本检查，
敞口和亏损熔断只拦开新仓的买入。
"""

from __future__ import annotations

import logging
import os
import time

from .clob import MarketInfo
from .config import RiskConfig
from .ledger import Ledger
from .signals import OrderIntent

logger = logging.getLogger(__name__)


def _short(text: str) -> str:
    return f"{text[:6]}…{text[-4:]}" if len(text) > 12 else text


def day_start_ts(now: float | None = None) -> float:
    """本地时区今天零点的 unix 时间（当日亏损熔断的口径）。"""
    local = time.localtime(now if now is not None else time.time())
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


class RiskGate:
    def __init__(self, config: RiskConfig, ledger: Ledger) -> None:
        self._config = config
        self._ledger = ledger
        self._blacklist = set(config.market_blacklist)

    def check(
        self, intent: OrderIntent, market: MarketInfo, target: str = ""
    ) -> tuple[bool, str]:
        """返回 (是否放行, 拦截原因)。

        target 是这笔跟单归属的目标地址（引擎从信号组传入），用于单目标单市场敞口闸；
        缺省空串时跳过该闸（其余检查不依赖目标）。
        """
        cfg = self._config
        if cfg.kill_switch_file and os.path.exists(cfg.kill_switch_file):
            return False, f"手动停机开关已打开（存在文件 {cfg.kill_switch_file}）"
        if market.closed:
            return False, "市场已关闭"
        if not market.accepting_orders:
            return False, "市场暂停接单"
        if (
            intent.condition_id.lower() in self._blacklist
            or (market.slug and market.slug.lower() in self._blacklist)
        ):
            return False, "市场在黑名单中"

        if intent.side != "BUY":
            return True, ""

        # 以下只拦开新仓
        if cfg.daily_max_loss_usdc is not None:
            today_pnl = self._ledger.realized_pnl_since(day_start_ts())
            if today_pnl <= -cfg.daily_max_loss_usdc:
                return False, (
                    f"当日已实现亏损 ${-today_pnl:.2f} 触发熔断"
                    f"（阈值 ${cfg.daily_max_loss_usdc:.2f}），今日停止开新仓"
                )
        if cfg.max_market_exposure_usdc is not None:
            market_cost = self._ledger.market_cost(intent.condition_id)
            if market_cost + intent.notional > cfg.max_market_exposure_usdc:
                return False, (
                    f"单市场敞口将达 ${market_cost + intent.notional:.2f}，"
                    f"超过上限 ${cfg.max_market_exposure_usdc:.2f}"
                )
        if cfg.max_market_exposure_per_target_usdc is not None and target:
            target_cost = self._ledger.target_market_net_cost(target, intent.condition_id)
            if target_cost + intent.notional > cfg.max_market_exposure_per_target_usdc:
                return False, (
                    f"目标 {_short(target)} 在市场 {_short(intent.condition_id)} 的敞口将达 "
                    f"${target_cost + intent.notional:.2f}，超过单目标单市场上限 "
                    f"${cfg.max_market_exposure_per_target_usdc:.2f}"
                )
        if cfg.max_total_exposure_usdc is not None:
            total_cost = self._ledger.total_cost()
            if total_cost + intent.notional > cfg.max_total_exposure_usdc:
                return False, (
                    f"总敞口将达 ${total_cost + intent.notional:.2f}，"
                    f"超过上限 ${cfg.max_total_exposure_usdc:.2f}"
                )
        return True, ""
