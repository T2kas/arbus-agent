"""Shared test safety net.

A pipeline test once wrote 61 fake markets into the real data/arbus.db, which
silently poisons the dedupe corpus for every future batch. Tests now get a
throwaway database by default and the production path is refused outright.
"""

import sqlite3
import sys
import types

import pytest

# feedparser's build fails on some platforms and no test needs it.
sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))

from arbus import config, store  # noqa: E402

_real_connect = store.connect


@pytest.fixture(autouse=True)
def never_touch_the_real_db(tmp_path, monkeypatch):
    def guarded(db_path: str = "", *args, **kwargs) -> sqlite3.Connection:
        if not db_path or db_path == config.DB_PATH:
            db_path = str(tmp_path / "test.db")
        return _real_connect(db_path)

    monkeypatch.setattr(store, "connect", guarded)
    yield
