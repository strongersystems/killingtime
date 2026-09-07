"""Snapshot persistence against a mocked Worker endpoint."""

from __future__ import annotations

import gzip
import os
import sqlite3

import httpx

from killingtime import state
from killingtime.config import Settings
from killingtime.db import connect


def test_persist_and_restore_roundtrip(tmp_path):
    store: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-kt-secret"] == "s3cret"
        assert request.url.path == "/_internal/db"
        if request.method == "PUT":
            store["blob"] = request.content
            return httpx.Response(200, json={"ok": True})
        if "blob" not in store:
            return httpx.Response(404)
        return httpx.Response(200, content=store["blob"], headers={"Content-Type": "application/gzip"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    db = tmp_path / "kt.db"
    s = Settings(_env_file=None, kt_db_path=str(db), kt_state_url="https://progress.example/", kt_state_secret="s3cret")
    assert state.configured(s)
    conn = connect(str(db))
    conn.execute("INSERT INTO meta(key, value) VALUES ('hello', 'world')")
    conn.commit()
    assert state.persist(s, http=http) is True
    assert gzip.decompress(store["blob"])[:16] == b"SQLite format 3\x00"
    # an existing database is never overwritten
    assert state.restore_if_missing(s, http=http) is False
    conn.close()
    os.remove(db)
    assert state.restore_if_missing(s, http=http) is True
    restored = sqlite3.connect(str(db))
    assert restored.execute("SELECT value FROM meta WHERE key = 'hello'").fetchone()[0] == "world"


def test_not_configured_is_noop(tmp_path):
    s = Settings(_env_file=None, kt_db_path=str(tmp_path / "x.db"))
    assert state.configured(s) is False
    assert state.persist(s) is False and state.restore_if_missing(s) is False


def test_restore_handles_missing_snapshot(tmp_path):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    s = Settings(_env_file=None, kt_db_path=str(tmp_path / "x.db"), kt_state_url="https://p", kt_state_secret="k")
    assert state.restore_if_missing(s, http=http) is False
    assert not os.path.exists(tmp_path / "x.db")
