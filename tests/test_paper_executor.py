from polycopycat.engine.clob import BookLevel, ClobError, OrderBook
from polycopycat.engine.executor import PaperExecutor
from polycopycat.engine.signals import OrderIntent


class FakeClob:
    def __init__(self, book=None, error=None):
        self.book = book
        self.error = error

    def get_book(self, token_id):
        if self.error:
            raise self.error
        return self.book


def intent(side="BUY", limit=0.52, size=50.0, ref=0.50):
    return OrderIntent(
        token_id="tok1", condition_id="0xcond", side=side, limit_price=limit,
        size=size, ref_price=ref, neg_risk=False,
    )


def book(bids=(), asks=()):
    return OrderBook(
        bids=tuple(BookLevel(*b) for b in bids),
        asks=tuple(BookLevel(*a) for a in asks),
    )


def test_buy_walks_asks_within_limit():
    executor = PaperExecutor(FakeClob(book(asks=[(0.51, 30), (0.52, 100), (0.53, 100)])))
    result = executor.execute(intent(size=50))
    assert result.status == "filled"
    assert result.filled_size == 50
    # 30@0.51 + 20@0.52 → avg 0.514
    assert abs(result.avg_price - 0.514) < 1e-9
    assert abs(result.slippage - 0.014) < 1e-9


def test_buy_partial_when_depth_short():
    executor = PaperExecutor(FakeClob(book(asks=[(0.51, 10)])))
    result = executor.execute(intent(size=50))
    assert result.status == "partial"
    assert result.filled_size == 10
    assert "剩余" in result.detail


def test_buy_rejected_when_no_liquidity_within_cap():
    executor = PaperExecutor(FakeClob(book(asks=[(0.55, 100)])))
    result = executor.execute(intent(limit=0.52))
    assert result.status == "rejected"
    assert "滑点保护" in result.detail
    assert not result.ok


def test_sell_walks_bids_and_slippage_sign():
    executor = PaperExecutor(FakeClob(book(bids=[(0.49, 30), (0.48, 100)])))
    result = executor.execute(intent(side="SELL", limit=0.48, size=50, ref=0.50))
    assert result.status == "filled"
    # 30@0.49 + 20@0.48 → avg 0.486，卖得比目标低 → 滑点 +0.014
    assert abs(result.avg_price - 0.486) < 1e-9
    assert abs(result.slippage - 0.014) < 1e-9


def test_book_error_becomes_error_result():
    executor = PaperExecutor(FakeClob(error=ClobError("boom")))
    result = executor.execute(intent())
    assert result.status == "error"
    assert "订单簿" in result.detail
