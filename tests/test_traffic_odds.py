"""Opening-probability model for the traffic round (pure math, no CV deps)."""

import math
import random

import pytest

from traffic import odds


def test_poisson_when_not_overdispersed():
    d = odds.fit(8.0, 10.0)            # 10 < 1.3 * 8
    assert d.kind == "poisson" and d.var == 8.0


def test_negbin_when_variance_exceeds_threshold():
    d = odds.fit(8.0, 20.0)
    assert d.kind == "negbin"
    total = sum(d.pmf(k) for k in range(400))
    mean = sum(k * d.pmf(k) for k in range(400))
    var = sum((k - mean) ** 2 * d.pmf(k) for k in range(400))
    assert total == pytest.approx(1, abs=1e-9)
    assert mean == pytest.approx(8.0, rel=1e-6)    # method of moments holds
    assert var == pytest.approx(20.0, rel=1e-4)


def test_poisson_pmf_matches_closed_form():
    d = odds.fit(3.0, 3.0)
    assert d.pmf(2) == pytest.approx(math.exp(-3) * 9 / 2)


def test_pool_equals_one_big_sample():
    rng = random.Random(1)
    a = [rng.randint(0, 20) for _ in range(17)]
    b = [rng.randint(5, 30) for _ in range(23)]
    pooled = odds.pool([odds.summarize(a), odds.summarize(b)])
    direct = odds.summarize(a + b)
    assert pooled[0] == direct[0]
    assert pooled[1] == pytest.approx(direct[1])
    assert pooled[2] == pytest.approx(direct[2])


def _row(wd, hr, n, mean=10.0, var=12.0, cam="c1"):
    return {"camera_id": cam, "weekday": wd, "hour": hr, "n": n, "mean": mean, "variance": var}


def test_segment_uses_exact_hour_when_enough_data():
    stats = [_row(0, 8, 40, mean=20), _row(0, 9, 40, mean=5)]
    n, m, _, level = odds.segment(stats, "c1", 0, 8)
    assert (n, m, level) == (40, 20, "valanda")


def test_segment_falls_back_to_neighbouring_hours_then_camera():
    stats = [_row(0, 8, 10), _row(0, 9, 25), _row(3, 14, 5), _row(0, 8, 99, cam="other")]
    n, _, _, level = odds.segment(stats, "c1", 0, 8)
    assert (n, level) == (35, "±1 val.")
    n, _, _, level = odds.segment(stats, "c1", 5, 2)
    assert (n, level) == (40, "visa kamera")           # never another camera's data


def test_segment_hours_wrap_around_midnight():
    stats = [_row(1, 23, 20), _row(1, 0, 20)]
    n, _, _, level = odds.segment(stats, "c1", 1, 0)
    assert (n, level) == (40, "±1 val.")


@pytest.mark.parametrize("mean,var", [(4, 4), (8, 8), (8, 20), (15, 40), (30, 30),
                                      (60, 200)])
def test_buckets_open_between_15_and_40_percent(mean, var):
    d = odds.fit(mean, var)
    lows = odds.choose_buckets(d)
    probs = odds.bucket_probs(d, lows)
    assert lows[0] == 0 and list(lows) == sorted(set(lows))
    assert sum(probs) == pytest.approx(1)
    assert all(0.15 - 1e-9 <= p <= 0.40 + 1e-9 for p in probs), (lows, probs)


def test_lumpy_small_mean_gets_the_closest_split():
    # lambda = 2: no integer split fits 15-40 % exactly; 0-1 / 2 / 3+ misses least
    d = odds.fit(2, 2)
    lows = odds.choose_buckets(d)
    assert lows == (0, 2, 3)
    assert all(0.13 <= p <= 0.42 for p in odds.bucket_probs(d, lows))


def test_empty_street_still_gives_two_outcomes():
    lows = odds.choose_buckets(odds.fit(0.2, 0.2))
    assert len(lows) >= 2


def test_floor_probs_clips_and_renormalizes():
    p = odds.floor_probs([0.9, 0.09, 0.005, 0.005])
    assert sum(p) == pytest.approx(1)
    assert min(p) >= 0.02 - 1e-12
    assert p[0] > p[1] > p[2]                  # order of the big ones is kept


def test_floor_probs_leaves_valid_book_alone():
    assert odds.floor_probs([0.25, 0.25, 0.3, 0.2]) == pytest.approx([0.25, 0.25, 0.3, 0.2])


def test_labels_and_bucket_of():
    lows = (0, 5, 9, 13)
    assert odds.labels(lows) == ["0–4", "5–8", "9–12", "13+"]
    assert [odds.bucket_of(c, lows) for c in (0, 4, 5, 12, 13, 99)] == [0, 0, 1, 2, 3, 3]
    assert odds.labels((0, 1, 2)) == ["0", "1", "2+"]


def test_lmsr_q_reproduces_prices_and_is_nonnegative():
    probs = [0.2, 0.3, 0.35, 0.15]
    q = odds.lmsr_q(probs, 1500)
    assert min(q) == 0
    assert odds.lmsr_prices(q, 1500) == pytest.approx(probs)


def test_liquidity_caps_price_move_for_typical_stake():
    probs = [0.19, 0.26, 0.26, 0.29]
    b = odds.liquidity_for(100, probs, max_move=0.05)
    for p in probs:
        assert odds.price_after_buy(p, 100, b) - p <= 0.05
    # and it is the smallest such b on the rounding grid
    worst = min(probs)
    assert odds.price_after_buy(worst, 100, b - 50) - worst > 0.05


def test_opening_book_shape():
    book = odds.opening_book(odds.fit(8, 8), typical_stake=100)
    assert sum(b["p"] for b in book["buckets"]) == pytest.approx(1, abs=1e-3)
    assert book["buckets"][-1]["hi"] is None
    assert book["liquidity"] >= 100 and min(book["q"]) == 0
