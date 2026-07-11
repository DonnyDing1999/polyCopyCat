"""候选来源与评估编排：收集地址 → 逐个回放打分 → 排序输出。"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable

import requests

from .._http import HttpError, get_json
from ..data_api import DataApiClient, DataApiError, normalize_address
from .metrics import replay
from .score import ScoutConfig, Verdict, evaluate

logger = logging.getLogger(__name__)

DEFAULT_LB_URL = "https://lb-api.polymarket.com"
ENV_LB_URL = "POLYCOPYCAT_LB_URL"


class ScoutError(RuntimeError):
    """候选收集或评估失败。"""


def candidates_from_leaderboard(
    base_url: str | None = None,
    *,
    session: requests.Session | None = None,
    window: str = "30d",
    rank_type: str = "pnl",
    limit: int = 50,
    timeout: float = 10.0,
) -> list[str]:
    """从官方排行榜取候选地址（按盈利或成交额排名）。

    排行榜接口不在正式文档里，字段做宽容解析；不可用时抛 ScoutError，
    由调用方降级到其他来源。
    """
    base = (base_url or os.environ.get(ENV_LB_URL) or DEFAULT_LB_URL).rstrip("/")
    if session is None:
        session = requests.Session()
        session.headers.update({"Accept": "application/json"})
    try:
        data = get_json(
            session, f"{base}/leaderboard",
            params={"window": window, "rankType": rank_type, "limit": limit},
            timeout=timeout,
        )
    except HttpError as exc:
        raise ScoutError(f"拉取排行榜失败: {exc}") from exc
    if not isinstance(data, list):
        raise ScoutError(f"排行榜返回了意外结构: {data!r:.200}")
    out: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw = item.get("proxyWallet") or item.get("wallet") or item.get("address") or ""
        try:
            out.append(normalize_address(str(raw)))
        except ValueError:
            continue
    return out


def candidates_from_recent_trades(
    client: DataApiClient,
    *,
    pages: int = 2,
    page_limit: int = 500,
    top: int = 40,
) -> list[str]:
    """从全站最近成交里挖活跃地址，按窗口内成交额排序取前 top 个。"""
    notional_by_wallet: dict[str, float] = {}
    for page in range(max(1, pages)):
        trades = client.get_recent_trades(limit=page_limit, offset=page * page_limit)
        for trade in trades:
            if trade.proxy_wallet:
                notional_by_wallet[trade.proxy_wallet] = (
                    notional_by_wallet.get(trade.proxy_wallet, 0.0) + trade.notional
                )
        if len(trades) < page_limit:
            break
    ranked = sorted(notional_by_wallet.items(), key=lambda kv: kv[1], reverse=True)
    return [wallet for wallet, _ in ranked[: max(1, top)]]


def scout_addresses(
    client: DataApiClient,
    addresses: list[str],
    *,
    config: ScoutConfig | None = None,
    pages: int = 1,
    now: float | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> list[Verdict]:
    """逐个评估地址，返回按（合格优先，分数降序）排序的结论。"""
    config = config or ScoutConfig()
    verdicts: list[Verdict] = []
    total = len(addresses)
    for index, address in enumerate(addresses):
        address = normalize_address(address)
        if progress:
            progress(address, index + 1, total)
        if index and config.request_delay_s > 0:
            time.sleep(config.request_delay_s)
        try:
            tape = []
            for page in range(max(1, pages)):
                chunk = client.get_trades(address, limit=500, offset=page * 500)
                tape.extend(chunk)
                if len(chunk) < 500:
                    break
            positions = client.get_positions(address)
        except DataApiError as exc:
            logger.warning("拉取 %s 数据失败，跳过: %s", address, exc)
            verdicts.append(Verdict(
                address=address, eligible=False, score=0.0,
                reasons=[f"数据拉取失败：{exc}"],
            ))
            continue
        stats = replay(address, tape, quick_window_s=config.quick_window_s)
        verdicts.append(evaluate(stats, positions, config, now=now))
    verdicts.sort(key=lambda v: (not v.eligible, -v.score))
    return verdicts


def targets_snippet(verdicts: list[Verdict], *, top: int = 5) -> str:
    """把前 top 个合格地址拼成可直接粘进 copycat.json 的 targets 段。"""
    rows = [
        {"address": v.address, "ratio": 0.1, "max_per_trade_usdc": 50}
        for v in verdicts if v.eligible
    ][: max(1, top)]
    return json.dumps({"targets": rows}, ensure_ascii=False, indent=2)
