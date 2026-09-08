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
import shutil
import sqlite3
import tempfile
import zlib

import httpx

from .config import Settings

log = logging.getLogger(__name__)
HEADER = "X-KT-Secret"


def configured(settings: Settings) -> bool:
    return bool(settings.kt_state_url and settings.kt_state_secret)


def _url(settings: Settings, path: str = "/_internal/db") -> str:
    return settings.kt_state_url.rstrip("/") + path


def publish_page(settings: Settings, html: str, http: httpx.Client | None = None, name: str = "public") -> bool:
    """Upload a rendered public page so the Worker can serve it without waking the container.

    ``name`` selects the page: "public" is the front page, "team" the Meet the Team page."""
    if not configured(settings):
        return False
    client = http or httpx.Client(timeout=60)
    path = "/_internal/public" if name == "public" else f"/_internal/public/{name}"
    try:
        resp = client.put(
            _url(settings, path), content=html.encode("utf-8"),
            headers={HEADER: settings.kt_state_secret, "Content-Type": "text/html; charset=utf-8"},
        )
    except httpx.HTTPError as exc:
        log.warning("%s page publish failed: %s", name, exc)
        return False
    if resp.status_code >= 300:
        log.warning("%s page publish failed: HTTP %s %s", name, resp.status_code, resp.text[:200])
        return False
    log.info("published %s page (%d bytes)", name, len(html))
    return True


def restore_if_missing(settings: Settings, http: httpx.Client | None = None) -> bool:
    """Download the last snapshot if no local database exists yet. Returns True when a snapshot was restored.

    Streamed and decompressed a chunk at a time, straight to disk. Reading the response body and then decompressing
    it holds two copies of the whole database in memory at once, which is fine for a small guild and fatal on a
    container with a gigabyte to its name: the process is killed before it ever opens a port, and every restart
    tries the same thing again.
    """
    if not configured(settings):
        return False
    path = settings.kt_db_path
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return False
    client = http or httpx.Client(timeout=httpx.Timeout(120.0, read=600.0))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".restore"
    written = 0
    try:
        with client.stream("GET", _url(settings), headers={HEADER: settings.kt_state_secret}) as resp:
            if resp.status_code == 404:
                log.info("no remote snapshot yet")
                return False
            if resp.status_code != 200:
                resp.read()
                log.warning("state restore failed: HTTP %s %s", resp.status_code, resp.text[:200])
                return False
            # wbits 16+MAX_WBITS reads a gzip container rather than a bare deflate stream.
            gz = zlib.decompressobj(16 + zlib.MAX_WBITS)
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    out = gz.decompress(chunk)
                    if out:
                        fh.write(out)
                        written += len(out)
                out = gz.flush()
                if out:
                    fh.write(out)
                    written += len(out)
    except (httpx.HTTPError, zlib.error, OSError) as exc:
        log.warning("state restore failed: %s", exc)
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    os.replace(tmp, path)
    log.info("restored database snapshot (%d bytes)", written)
    return True


def persist(settings: Settings, http: httpx.Client | None = None) -> bool:
    """Upload a consistent, gzipped snapshot of the database. Returns True on success."""
    if not configured(settings):
        return False
    path = settings.kt_db_path
    dirname = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(suffix=".db", dir=dirname)
    os.close(fd)
    os.remove(tmp)  # VACUUM INTO needs a non-existent target
    gz_path = tmp + ".gz"
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute("VACUUM INTO ?", (tmp,))
        finally:
            conn.close()
        # Compress through the filesystem rather than through memory, for the same reason the restore streams.
        with open(tmp, "rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        size = os.path.getsize(gz_path)
        client = http or httpx.Client(timeout=httpx.Timeout(120.0, write=600.0, read=600.0))
        try:
            with open(gz_path, "rb") as body:
                resp = client.put(
                    _url(settings), content=body,
                    headers={HEADER: settings.kt_state_secret, "Content-Type": "application/gzip",
                             "Content-Length": str(size)},
                )
        except httpx.HTTPError as exc:
            log.warning("state persist failed: %s", exc)
            return False
    finally:
        for f in (tmp, gz_path):
            if os.path.exists(f):
                os.remove(f)
    if resp.status_code >= 300:
        log.warning("state persist failed: HTTP %s %s", resp.status_code, resp.text[:200])
        return False
    log.info("persisted database snapshot (%d bytes gzipped)", size)
    return True
