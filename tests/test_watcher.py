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


def test_ingest_emits_once_and_dedupes_against_polling():
    page = [make_trade("0x1", 100)]
    client = FakeClient({A1: [[], page, page]})
    emitted = []
    watcher = TradeWatcher(client, [A1], on_trade=emitted.append)
    watcher.poll_once()  # 基线（空页）

    live = make_trade("0x1", 100)
    assert watcher.ingest(live) is True    # 实时通道先到，触发回调
    assert watcher.ingest(live) is False   # 重复推送不再触发
    assert watcher.poll_once() == []       # 轮询看到同一笔也不再上报
    assert [t.transaction_hash for t in emitted] == ["0x1"]


def test_ingest_ignores_unwatched_wallet():
    watcher = TradeWatcher(FakeClient({A1: [[]]}), [A1])
    assert watcher.ingest(make_trade("0x9", 100, wallet=A2)) is False


def test_request_poll_sets_wake_event():
    watcher = TradeWatcher(FakeClient({A1: [[]]}), [A1])
    assert not watcher._wake.is_set()
    watcher.request_poll()
    assert watcher._wake.is_set()


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


def test_add_address_baselines_before_reporting():
    """动态加入的地址：老成交只建基线，不刷成新信号；之后的新成交正常上报。"""
    a3 = "0x" + "f" * 40
    old_tape = [make_trade("0xold2", 200, wallet=a3), make_trade("0xold1", 100, wallet=a3)]
    client = FakeClient({
        A1: [[make_trade("0x1", 100)]],
        a3: [old_tape, [make_trade("0xnew", 300, wallet=a3)] + old_tape],
    })
    got = []
    watcher = TradeWatcher(client, [A1], on_trade=got.append)
    watcher.poll_once()  # A1 建基线

    assert watcher.add_address(a3) is True
    assert watcher.add_address(a3) is False  # 重复加返回 False
    watcher.poll_once()  # a3 首轮：只建基线，老成交不上报
    assert got == []

    watcher.poll_once()  # a3 出现新成交
    assert [t.transaction_hash for t in got] == ["0xnew"]


def test_signal_source_tagging_per_channel():
    """轮询→poll、实时→stream、回放→backfill；跨通道仍去重。"""
    tape = [make_trade("0x2", 200), make_trade("0x1", 100)]
    client = FakeClient({A1: [tape, [make_trade("0x3", 300)] + tape]})
    got = []
    watcher = TradeWatcher(client, [A1], on_trade=got.append, backfill=1)
    watcher.poll_once()   # 基线 + 回放 1 条
    assert [t.source for t in got] == ["backfill"]

    # 实时通道先推 0x3
    assert watcher.ingest(make_trade("0x3", 300)) is True
    assert got[-1].source == "stream" and got[-1].transaction_hash == "0x3"

    watcher.poll_once()   # 轮询再看到 0x3：已见，不重复上报
    assert len(got) == 2

    # 纯轮询发现的新成交带 poll 标
    client.pages[A1] = [[make_trade("0x4", 400)] + tape]
    watcher.poll_once()
    assert got[-1].source == "poll" and got[-1].transaction_hash == "0x4"
