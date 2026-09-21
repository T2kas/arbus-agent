"""Offline tests for themed chunk allocation."""

import sys
import types

sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))

from arbus import config  # noqa: E402
from arbus.pipeline import _theme_chunks  # noqa: E402


def test_allocation_totals_requested_count():
    for count in (5, 15, 35, 7):
        assert sum(n for n, _, _ in _theme_chunks(count, 15)) == count


def test_every_theme_represented_in_a_full_batch():
    labels = {label for _, label, _ in _theme_chunks(35, 15)}
    assert labels == {label for label, _, _ in config.DRAFT_THEMES}


def test_culture_and_sport_dominate_over_politics():
    """Team direction 2026-09-21: the batch should lead with fun culture/sport/
    internet markets, not politics/economics."""
    chunks = _theme_chunks(35, 15)
    per_theme = {}
    for n, label, _ in chunks:
        per_theme[label] = per_theme.get(label, 0) + n
    fun = (per_theme["kultūra ir pramogos"] + per_theme["sportas"]
           + per_theme["influenceriai ir internetas"])
    informative = (per_theme["valstybė ir aktualijos"]
                   + per_theme["kasdienybė ir ekonomika"])
    assert fun > informative


def test_small_batch_is_not_all_one_theme():
    """A tiny batch used to dump every slot on the first theme (all politics)."""
    labels = {label for _, label, _ in _theme_chunks(3, 15)}
    assert len(labels) == 3


def test_chunks_never_exceed_chunk_size():
    assert all(n <= 8 for n, _, _ in _theme_chunks(35, 8))


def test_mandate_text_travels_with_each_chunk():
    for _, label, focus in _theme_chunks(35, 15):
        assert focus.startswith("ONLY")
        assert len(focus) > 50, label
