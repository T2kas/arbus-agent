"""Opening probabilities for a round's count intervals.

Built ONLY from historical statistics of (camera, weekday, hour) — never from
the clip that will be played; this module has no access to clip results.

* Poisson with lambda = mean; negative binomial (method of moments) when the
  variance exceeds 1.3 x mean.
* Segments with < 30 observations fall back to neighbouring hours, then to the
  camera's whole distribution.
* Interval bounds follow the distribution's quantiles so each interval opens at
  roughly 15-40 %; no interval below 2 % (clip + renormalize).
* The Arbus engine is LMSR-style: a market's state is its option probabilities
  plus liquidity b (a buy of net s multiplies the bought outcome's weight by
  e^(s/b)). Opening at p_i is therefore q_i = b*ln(p_i) shifted to min 0, and b
  is sized so a typical stake moves a price by at most ~5 p.p.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import product
from typing import Iterable, Sequence

OVERDISPERSION = 1.3
MIN_SEGMENT_N = 30


@dataclass(frozen=True)
class Dist:
    kind: str          # "poisson" | "negbin"
    mean: float
    var: float

    @property
    def nb_r(self) -> float:
        return self.mean ** 2 / (self.var - self.mean)

    @property
    def nb_p(self) -> float:
        return self.mean / self.var

    def logpmf(self, k: int) -> float:
        if k < 0:
            return -math.inf
        m = max(self.mean, 1e-9)
        if self.kind == "poisson":
            return k * math.log(m) - m - math.lgamma(k + 1)
        r, p = self.nb_r, self.nb_p
        return (math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                + r * math.log(p) + k * math.log1p(-p))

    def pmf(self, k: int) -> float:
        return math.exp(self.logpmf(k))

    def cdf(self, k: int) -> float:
        return min(1.0, sum(self.pmf(i) for i in range(0, k + 1))) if k >= 0 else 0.0

    def upper(self, tail: float = 1e-4, cap: int = 2000) -> int:
        """Smallest k with P(X > k) < tail."""
        acc = 0.0
        for k in range(cap):
            acc += self.pmf(k)
            if 1 - acc < tail:
                return k
        return cap


def fit(mean: float, var: float, overdispersion: float = OVERDISPERSION) -> Dist:
    mean = max(float(mean), 1e-6)
    var = float(var)
    if var > overdispersion * mean:
        return Dist("negbin", mean, var)
    return Dist("poisson", mean, mean)


def summarize(counts: Sequence[int]) -> tuple[int, float, float]:
    """(n, mean, sample variance) of observed counts."""
    n = len(counts)
    if n == 0:
        return 0, 0.0, 0.0
    m = sum(counts) / n
    v = sum((c - m) ** 2 for c in counts) / (n - 1) if n > 1 else 0.0
    return n, m, v


def pool(segments: Iterable[tuple[int, float, float]]) -> tuple[int, float, float]:
    """Combine (n, mean, sample variance) groups exactly, as if one sample."""
    segs = [s for s in segments if s[0] > 0]
    n = sum(s[0] for s in segs)
    if n == 0:
        return 0, 0.0, 0.0
    m = sum(s[0] * s[1] for s in segs) / n
    ss = sum((s[0] - 1) * s[2] + s[0] * (s[1] - m) ** 2 for s in segs)
    return n, m, ss / (n - 1) if n > 1 else 0.0


def segment(stats: Sequence[dict], camera_id: str, weekday: int, hour: int,
            min_n: int = MIN_SEGMENT_N) -> tuple[int, float, float, str]:
    """Pick the narrowest (camera, weekday, hour) segment with enough data.

    `stats` rows: {camera_id, weekday (0=Mon), hour, n, mean, variance}.
    Returns (n, mean, var, level) — level says which fallback was used.
    """
    rows = [s for s in stats if s["camera_id"] == camera_id]

    def near(h: int, d: int) -> bool:
        return min((h - hour) % 24, (hour - h) % 24) <= d

    levels = [
        ("valanda", lambda s: s["weekday"] == weekday and s["hour"] == hour),
        ("±1 val.", lambda s: s["weekday"] == weekday and near(s["hour"], 1)),
        ("±2 val.", lambda s: s["weekday"] == weekday and near(s["hour"], 2)),
        ("visos dienos ±1 val.", lambda s: near(s["hour"], 1)),
        ("visa kamera", lambda s: True),
    ]
    for name, keep in levels:
        n, m, v = pool((s["n"], s["mean"], s["variance"]) for s in rows if keep(s))
        if n >= min_n or name == "visa kamera":
            return n, m, v, name
    raise AssertionError("unreachable")


def bucket_probs(dist: Dist, lows: Sequence[int]) -> list[float]:
    """P(count in each interval); intervals start at `lows`, the last is open."""
    cdf_before = [dist.cdf(lo - 1) for lo in lows] + [1.0]
    return [max(cdf_before[i + 1] - cdf_before[i], 0.0) for i in range(len(lows))]


def choose_buckets(dist: Dist, n: int = 4, lo: float = 0.15, hi: float = 0.40) -> tuple[int, ...]:
    """Interval lower bounds near the distribution's quantiles.

    Tries n intervals, then n-1, then n+1, until every interval's probability is
    within [lo, hi]; otherwise returns the split that misses that band least.
    """
    kmax = dist.upper()
    cdf = []
    acc = 0.0
    for k in range(kmax + 1):
        acc += dist.pmf(k)
        cdf.append(acc)

    def candidates(q: float) -> list[int]:
        # cut point c means the interval starts at c, i.e. P(X < c) = cdf[c-1]
        out = [c for c in range(1, kmax + 1) if abs(cdf[c - 1] - q) <= 0.12]
        nearest = min(range(1, kmax + 1), key=lambda c: abs(cdf[c - 1] - q))
        return out if nearest in out else [*out, nearest]

    least_bad = None   # (violation, score, lows) over every interval count tried
    for m in (n, n - 1, n + 1):
        if m < 2 or kmax < 1:
            continue
        best = None
        for cuts in product(*(candidates(i / m) for i in range(1, m))):
            if any(b <= a for a, b in zip(cuts, cuts[1:])):
                continue
            lows = (0, *cuts)
            edges = [0.0, *(cdf[c - 1] for c in cuts), 1.0]
            probs = [edges[i + 1] - edges[i] for i in range(m)]
            violation = sum(max(lo - p, 0) + max(p - hi, 0) for p in probs)
            score = sum((p - 1 / m) ** 2 for p in probs)
            if best is None or (violation, score) < best[:2]:
                best = (violation, score, lows)
        if best and best[0] < 1e-9:
            return best[2]
        if best and (least_bad is None or best[:2] < least_bad[:2]):
            least_bad = best
    # small means are lumpy (P(0), P(1) are big): no split may fit exactly,
    # so take the one closest to the target band
    if least_bad:
        return least_bad[2]
    return (0, 1)   # near-empty street: "0" vs "1+"


def floor_probs(probs: Sequence[float], floor: float = 0.02) -> list[float]:
    """Clip each probability to >= floor and renormalize, keeping sum = 1."""
    k = len(probs)
    if k * floor > 1:
        raise ValueError("floor too high for this many intervals")
    p = [max(x, 0.0) for x in probs]
    s = sum(p) or 1.0
    p = [x / s for x in p]
    fixed: set[int] = set()
    while True:
        low = [i for i, x in enumerate(p) if x < floor - 1e-12 and i not in fixed]
        if not low:
            return p
        fixed.update(low)
        free = [i for i in range(k) if i not in fixed]
        rest = 1 - floor * len(fixed)
        free_sum = sum(p[i] for i in free)
        p = [floor if i in fixed else p[i] * rest / free_sum for i in range(k)]


def labels(lows: Sequence[int]) -> list[str]:
    out = []
    for i, lo in enumerate(lows):
        if i + 1 < len(lows):
            hi = lows[i + 1] - 1
            out.append(str(lo) if hi == lo else f"{lo}–{hi}")
        else:
            out.append(f"{lo}+")
    return out


def bucket_of(count: int, lows: Sequence[int]) -> int:
    idx = 0
    for i, lo in enumerate(lows):
        if count >= lo:
            idx = i
    return idx


# ── LMSR ──────────────────────────────────────────────────────────────────

def lmsr_q(probs: Sequence[float], b: float) -> list[float]:
    """Outstanding shares that price the book at `probs`: q_i = b ln p_i, min 0."""
    q = [b * math.log(p) for p in probs]
    m = min(q)
    return [x - m for x in q]


def lmsr_prices(q: Sequence[float], b: float) -> list[float]:
    m = max(q)
    e = [math.exp((x - m) / b) for x in q]
    s = sum(e)
    return [x / s for x in e]


def price_after_buy(p: float, stake: float, b: float) -> float:
    """The Arbus engine's new price for an outcome after buying `stake` of it."""
    g = math.exp(stake / b)
    return (g - 1 + p) / g


def liquidity_for(typical_stake: float, probs: Sequence[float], max_move: float = 0.05,
                  round_to: int = 50) -> int:
    """Smallest b (rounded up) so `typical_stake` moves no price more than max_move.

    The move (1-p)(1-e^(-s/b)) is largest for the cheapest outcome, so size b on
    min(probs).
    """
    p = min(probs)
    b = typical_stake / -math.log(1 - max_move / (1 - p))
    return int(math.ceil(b / round_to) * round_to)


def opening_book(dist: Dist, typical_stake: float = 100, n_buckets: int = 4) -> dict:
    lows = choose_buckets(dist, n_buckets)
    probs = floor_probs(bucket_probs(dist, lows))
    b = liquidity_for(typical_stake, probs)
    return {
        "distribution": {"kind": dist.kind, "mean": round(dist.mean, 3), "var": round(dist.var, 3)},
        "buckets": [{"label": lab, "lo": lo, "hi": (lows[i + 1] - 1 if i + 1 < len(lows) else None),
                     "p": round(p, 4)}
                    for i, (lab, lo, p) in enumerate(zip(labels(lows), lows, probs))],
        "liquidity": b,
        "q": [round(x, 3) for x in lmsr_q(probs, b)],
    }
