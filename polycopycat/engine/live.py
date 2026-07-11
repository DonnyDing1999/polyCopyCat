"""实盘执行器：通过官方 py-clob-client 在 Polymarket CLOB 下 FAK 限价单。

需要安装 live 依赖：``pip install "polycopycat[live]"``。

安全约定：
- 私钥只从环境变量读（变量名在 live.private_key_env 配置），绝不落盘、绝不打日志；
- 建议使用专用小额热钱包；
- 实盘成交明细不立即写入账本持仓（applies_fills=False），以对账为准——
  post_order 的响应只说明订单被接受/撮合，不含逐档成交均价。
"""

from __future__ import annotations

import logging
import os

from .clob import DEFAULT_CLOB_URL, ENV_CLOB_URL
from .config import EngineConfig
from .executor import ExecutionResult
from .signals import OrderIntent

logger = logging.getLogger(__name__)

_TICK_LITERALS = {"0.1", "0.01", "0.001", "0.0001"}
_POLYGON_CHAIN_ID = 137


class LiveExecutor:
    mode = "live"
    applies_fills = False  # 实盘持仓以对账为准，见模块 docstring

    def __init__(self, config: EngineConfig, *, host: str | None = None, client=None) -> None:
        self._config = config
        self._client = client if client is not None else self._build_client(config, host)

    @staticmethod
    def _build_client(config: EngineConfig, host: str | None):
        try:
            from py_clob_client.client import ClobClient
        except ImportError as exc:
            raise RuntimeError(
                "实盘模式需要 py-clob-client，请先安装：pip install 'polycopycat[live]'"
            ) from exc
        live = config.live
        if not live.i_understand_live_trading_risk:
            raise RuntimeError(
                "实盘模式有真实资金风险：确认理解后把配置中的 "
                "live.i_understand_live_trading_risk 改成 true 再启动"
                "（或加 --paper 先跑纸面）"
            )
        private_key = os.environ.get(live.private_key_env, "").strip()
        if not private_key:
            raise RuntimeError(
                f"环境变量 {live.private_key_env} 未设置私钥（实盘模式必需）"
            )
        if live.signature_type in (1, 2) and not live.funder:
            raise RuntimeError(
                "signature_type 为 1/2（代理钱包）时必须配置 live.funder"
                "（资金所在的 proxy wallet 地址）"
            )
        resolved = (
            host or config.clob_url or os.environ.get(ENV_CLOB_URL) or DEFAULT_CLOB_URL
        )
        kwargs: dict = {"key": private_key, "chain_id": _POLYGON_CHAIN_ID}
        if live.signature_type != 0:
            kwargs["signature_type"] = live.signature_type
            kwargs["funder"] = live.funder
        client = ClobClient(resolved, **kwargs)
        client.set_api_creds(client.create_or_derive_api_creds())
        logger.info("实盘 CLOB 客户端就绪（signature_type=%d）", live.signature_type)
        return client

    def execute(self, intent: OrderIntent) -> ExecutionResult:
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions

        tick_literal = f"{intent.tick_size:g}"
        options = PartialCreateOrderOptions(
            tick_size=tick_literal if tick_literal in _TICK_LITERALS else None,
            neg_risk=intent.neg_risk,
        )
        try:
            signed = self._client.create_order(
                OrderArgs(
                    token_id=intent.token_id,
                    price=round(intent.limit_price, 4),
                    size=round(intent.size, 2),
                    side=intent.side,
                ),
                options,
            )
            resp = self._client.post_order(signed, OrderType.FAK)
        except Exception as exc:  # noqa: BLE001 —— 下单失败必须变成结果而不是崩溃
            return ExecutionResult(status="error", detail=f"实盘下单失败: {exc}")
        return self._parse_response(resp)

    @staticmethod
    def _parse_response(resp) -> ExecutionResult:
        if not isinstance(resp, dict):
            return ExecutionResult(status="error", detail=f"CLOB 返回了未知响应: {resp!r}")
        if not resp.get("success"):
            return ExecutionResult(
                status="error",
                detail=f"下单被拒: {resp.get('errorMsg') or resp}",
            )
        order_id = str(resp.get("orderID") or "")
        status = str(resp.get("status") or "")
        return ExecutionResult(
            status="submitted",
            detail=f"orderID={order_id or '?'} status={status or '?'}",
        )
