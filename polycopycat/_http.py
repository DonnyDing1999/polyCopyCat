"""带重试的 HTTP GET 小工具，Data API 与 CLOB 客户端共用。"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    """请求重试后仍失败或返回不可用数据。"""


def _send_with_retries(send, url: str, *, max_retries: int, backoff: float) -> Any:
    last_error: Exception | None = None
    for attempt in range(max(1, max_retries)):
        if attempt:
            time.sleep(backoff * (2 ** (attempt - 1)))
        try:
            response = send()
        except requests.RequestException as exc:
            last_error = exc
            logger.debug("请求 %s 失败（第 %d 次）: %s", url, attempt + 1, exc)
            continue
        if response.status_code in RETRYABLE_STATUS:
            last_error = HttpError(f"HTTP {response.status_code}")
            logger.debug(
                "请求 %s 返回 %d（第 %d 次），准备重试",
                url, response.status_code, attempt + 1,
            )
            continue
        try:
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            raise HttpError(f"服务端返回错误: {exc}") from exc
        except ValueError as exc:
            raise HttpError(f"返回了无法解析的 JSON: {exc}") from exc
    raise HttpError(f"请求 {url} 连续 {max_retries} 次失败: {last_error}") from last_error


def get_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 10.0,
    max_retries: int = 3,
    backoff: float = 1.0,
) -> Any:
    """GET 并解析 JSON；429/5xx 与网络错误按指数退避重试。"""
    return _send_with_retries(
        lambda: session.get(url, params=params, timeout=timeout),
        url, max_retries=max_retries, backoff=backoff,
    )
