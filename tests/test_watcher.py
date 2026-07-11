import pytest

from polycopycat.data_api import DataApiError
from polycopycat.models import Trade
from polycopycat.watcher import TradeWatcher

A1 = "0x" + "a" * 40
A2 = "0x" + "b" * 40


def make_trade(tx, ts, wallet=A1, side="BUY", size=10.0, price=0.5):
    return Trade(
        proxy_wallet=wallet,
        side=side,
        asset="123",
        condition_id="0xcond",
        size=size,
        price=price,
        timestamp=ts,
        title="测试市场",
        transaction_hash=tx,
    )


class FakeClient:
    """按调用顺序吐出预设页；页用完后一直重复最后一页。"""

    def __init__(self, pages_by_addr):
        self.pages = {addr: list(pages) for addr, pages in pages_by_addr.items()}

    def get_trades(self, user, *, limit=100, **kwargs):
        pages = self.pages[user]
        page = pages.pop(0) if len(pages) > 1 else pages[0]
        if isinstance(page, Exception):
            raise page
        return page[:limit]


def test_requires_at_least_one_address():
    with pytest.raises(ValueError):
        TradeWatcher(FakeClient({}), [])


def test_first_poll_only_baselines():
    client = FakeClient({A1: [[make_trade("0x2", 200), make_trade("0x1", 100)]]})
    watcher = TradeWatcher(client, [A1])
    assert watcher.poll_once() == []
    # 老成交不会在后续轮询里被再次当成新的
    assert watcher.poll_once() == []


def test_new_trades_emitted_ascending_and_deduped():
    old = [make_trade("0x2", 200), make_trade("0x1", 100)]
    newer = [make_trade("0x4", 400), make_trade("0x3", 300)] + old
    client = FakeClient({A1: [old, newer, newer]})
    seen = []
    watcher = TradeWatcher(client, [A1], on_trade=seen.append)

    watcher.poll_once()  # 基线
    fresh = watcher.poll_once()
    assert [t.transaction_hash for t in fresh] == ["0x3", "0x4"]  # 按时间升序
    assert [t.transaction_hash for t in seen] == ["0x3", "0x4"]  # 回调同步触发
    assert watcher.poll_once() == []  # 重复页不再上报


def test_backfill_replays_recent_history():
    page = [make_trade("0x3", 300), make_trade("0x2", 200), make_trade("0x1", 100)]
    client = FakeClient({A1: [page]})
    watcher = TradeWatcher(client, [A1], backfill=2)
    fresh = watcher.poll_once()
    assert [t.transaction_hash for t in fresh] == ["0x2", "0x3"]  # 最近 2 条，升序
    assert watcher.poll_once() == []


def test_multiple_addresses_merged_by_time():
    client = FakeClient(
        {
            A1: [[], [make_trade("0xa", 300, wallet=A1)]],
            A2: [[], [make_trade("0xb", 250, wallet=A2)]],
        }
    )
    watcher = TradeWatcher(client, [A1, A2, A1])  # 重复地址会被去重
    assert watcher.addresses == [A1, A2]
    watcher.poll_once()  # 基线
    fresh = watcher.poll_once()
    assert [t.transaction_hash for t in fresh] == ["0xb", "0xa"]


def test_api_error_skips_address_but_not_others():
    client = FakeClient(
        {
            A1: [DataApiError("boom")],
            A2: [[], [make_trade("0xb", 100, wallet=A2)]],
        }
    )
    watcher = TradeWatcher(client, [A1, A2])
    watcher.poll_once()  # A1 失败被跳过，A2 建立基线
    fresh = watcher.poll_once()
    assert [t.transaction_hash for t in fresh] == ["0xb"]
