from polycopycat.us import UsMarket, match_us_markets, score_match, tokenize


class FakeUsClient:
    """只实现 search_markets 的假客户端，按调用顺序吐预置结果。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.queries = []

    def search_markets(self, query, *, status="active", limit=None):
        self.queries.append(query)
        return self.pages.pop(0) if self.pages else []


def mk(slug, title, outcome="Yes", event_title="", volume=0.0):
    return UsMarket(
        id=1, slug=slug, title=title, outcome=outcome,
        event_title=event_title, volume=volume,
    )


def test_tokenize_slug_stopwords_numbers():
    assert tokenize("will-btc-hit-$100k-by-2026") == {"btc", "hit", "100000", "2026"}


def test_tokenize_number_variants_align():
    assert tokenize("$100,000") == tokenize("100k") == {"100000"}
    assert tokenize("1.5m") == {"1500000"}


def test_score_prefers_matching_numbers():
    query = tokenize("Bitcoin above $100k by Dec 31")
    right = mk("a", "Bitcoin above $100,000")
    wrong = mk("b", "Bitcoin above $150,000")
    assert score_match(query, right, None) > score_match(query, wrong, None)


def test_score_outcome_bonus():
    query = tokenize("Chiefs win the Super Bowl")
    market_yes = mk("a", "Chiefs win the Super Bowl", outcome="Yes")
    market_no = mk("b", "Chiefs win the Super Bowl", outcome="No")
    assert score_match(query, market_yes, "Yes") > score_match(query, market_no, "Yes")


def test_score_empty_tokens_is_zero():
    assert score_match(set(), mk("a", "Anything"), None) == 0.0
    assert score_match({"btc"}, mk("a", ""), None) == 0.0


def test_match_ranks_and_limits():
    client = FakeUsClient([
        [
            mk("btc-150k", "Bitcoin above $150,000"),
            mk("btc-100k", "Bitcoin above $100,000"),
            mk("eth-5k", "Ethereum above $5,000"),
        ]
    ])
    matches = match_us_markets(client, "Bitcoin above $100k", top=2)
    assert len(matches) == 2
    assert matches[0].market.slug == "btc-100k"
    assert matches[0].score > matches[1].score


def test_match_fallback_retries_with_fewer_words():
    target = mk("btc-100k", "Bitcoin above $100,000")
    client = FakeUsClient([[], [target]])
    matches = match_us_markets(
        client, "Will Bitcoin the largest cryptocurrency close above $100k before December 2026?"
    )
    assert len(client.queries) == 2
    assert len(client.queries[1].split()) <= 4
    assert matches and matches[0].market.slug == "btc-100k"


def test_match_dedups_by_slug():
    market = mk("btc-100k", "Bitcoin above $100,000")
    client = FakeUsClient([[market, market]])
    matches = match_us_markets(client, "bitcoin 100k")
    assert len(matches) == 1


def test_match_empty_text_returns_nothing():
    client = FakeUsClient([[mk("a", "Anything")]])
    assert match_us_markets(client, "---") == []
    assert client.queries == []  # 连搜索都不该发


def test_match_to_dict_flat():
    client = FakeUsClient([[mk("btc-100k", "Bitcoin above $100,000", volume=42.0)]])
    data = match_us_markets(client, "bitcoin 100k")[0].to_dict()
    assert data["slug"] == "btc-100k"
    assert data["volume"] == 42.0
    assert "score" in data
