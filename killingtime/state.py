"""Persist the SQLite database to the Cloudflare Worker (Durable Object storage) between container restarts.

Container disks on Cloudflare are ephemeral. When ``KT_STATE_URL`` and ``KT_STATE_SECRET`` are set, the app
restores the database from ``<KT_STATE_URL>/_internal/db`` at startup (if the local file is missing) and uploads a
gzipped snapshot after every successful sync. The Worker stores the blob in its Durable Object's SQLite storage,
so no R2/KV bindings or extra API tokens are required.
"""

from __future__ import annotations

import gzip
import logging
import os
import sqlite3
import tempfile

import httpx

from .config import Settings

log = logging.getLogger(__name__)
HEADER = "X-KT-Secret"


def configured(settings: Settings) -> bool:
    return bool(settings.kt_state_url and settings.kt_state_secret)


def _url(settings: Settings, path: str = "/_internal/db") -> str:
    return settings.kt_state_url.rstrip("/") + path


def publish_page(settings: Settings, html: str, http: httpx.Client | None = None) -> bool:
    """Upload the rendered public site so the Worker can serve it without waking the container."""
    if not configured(settings):
        return False
    client = http or httpx.Client(timeout=60)
    try:
        resp = client.put(
            _url(settings, "/_internal/public"), content=html.encode("utf-8"),
            headers={HEADER: settings.kt_state_secret, "Content-Type": "text/html; charset=utf-8"},
        )
    except httpx.HTTPError as exc:
        log.warning("public page publish failed: %s", exc)
        return False
    if resp.status_code >= 300:
        log.warning("public page publish failed: HTTP %s %s", resp.status_code, resp.text[:200])
        return False
    log.info("published public page (%d bytes)", len(html))
    return True


def restore_if_missing(settings: Settings, http: httpx.Client | None = None) -> bool:
    """Download the last snapshot if no local database exists yet. Returns True when a snapshot was restored."""
    if not configured(settings):
        return False
    path = settings.kt_db_path
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return False
    client = http or httpx.Client(timeout=120)
    try:
        resp = client.get(_url(settings), headers={HEADER: settings.kt_state_secret})
    except httpx.HTTPError as exc:
        log.warning("state restore failed: %s", exc)
        return False
    if resp.status_code == 404:
        log.info("no remote snapshot yet")
        return False
    if resp.status_code != 200:
        log.warning("state restore failed: HTTP %s %s", resp.status_code, resp.text[:200])
        return False
    data = gzip.decompress(resp.content)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".restore"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    log.info("restored database snapshot (%d bytes)", len(data))
    return True


def persist(settings: Settings, http: httpx.Client | None = None) -> bool:
    """Upload a consistent, gzipped snapshot of the database. Returns True on success."""
    if not configured(settings):
        return False
    path = settings.kt_db_path
    fd, tmp = tempfile.mkstemp(suffix=".db", dir=os.path.dirname(os.path.abspath(path)))
    os.close(fd)
    os.remove(tmp)  # VACUUM INTO needs a non-existent target
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute("VACUUM INTO ?", (tmp,))
        finally:
            conn.close()
        with open(tmp, "rb") as fh:
            blob = gzip.compress(fh.read(), compresslevel=6)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    client = http or httpx.Client(timeout=300)
    try:
        resp = client.put(
            _url(settings), content=blob,
            headers={HEADER: settings.kt_state_secret, "Content-Type": "application/gzip"},
        )
    except httpx.HTTPError as exc:
        log.warning("state persist failed: %s", exc)
        return False
    if resp.status_code >= 300:
        log.warning("state persist failed: HTTP %s %s", resp.status_code, resp.text[:200])
        return False
    log.info("persisted database snapshot (%d bytes gzipped)", len(blob))
    return True
