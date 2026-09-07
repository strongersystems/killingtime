"""Raider.IO public API client (no authentication required).

Used for cross-guild comparisons: realm leaderboards include every guild's first-kill
times, pull counts and best percentages, so rivals don't need to publish their logs.

Endpoints used:
    GET /api/v1/guilds/profile?region&realm&name&fields=raid_progression,raid_rankings
    GET /api/v1/raiding/static-data?expansion_id=N
    GET /api/v1/mythic-plus/static-data?expansion_id=N  (season start/end dates)
    GET /api/v1/raiding/raid-rankings?raid&difficulty&region&realm&limit&page
    GET /api/v1/guilds/boss-kill?region&realm&guild&raid&boss&difficulty
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class RaiderIOError(RuntimeError):
    pass


class RaiderIOClient:
    def __init__(
        self,
        base_url: str = "https://raider.io/api/v1",
        http: httpx.Client | None = None,
        timeout: float = 60.0,
        min_interval_s: float = 0.35,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout, headers={"User-Agent": "killingtime-progress/0.1"})
        self._min_interval = min_interval_s
        self._last_call = 0.0
        self.requests_made = 0

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        # Be polite: Raider.IO rate-limits aggressive anonymous clients.
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        resp = self._http.get(f"{self.base_url}{path}", params=params)
        self._last_call = time.monotonic()
        self.requests_made += 1
        if resp.status_code == 429:
            retry = float(resp.headers.get("retry-after", "5"))
            log.warning("Raider.IO rate limited; sleeping %.0fs", retry)
            time.sleep(min(retry, 30))
            resp = self._http.get(f"{self.base_url}{path}", params=params)
        if resp.status_code == 400:
            raise RaiderIOError(f"Raider.IO bad request for {path}: {resp.text[:200]}")
        if resp.status_code != 200:
            raise RaiderIOError(f"Raider.IO HTTP {resp.status_code} for {path}: {resp.text[:200]}")
        return resp.json()

    def guild_profile(self, region: str, realm: str, name: str, fields: str = "raid_progression,raid_rankings") -> dict:
        return self._get("/guilds/profile", {"region": region, "realm": realm, "name": name, "fields": fields})

    def static_data(self, expansion_id: int) -> dict:
        return self._get("/raiding/static-data", {"expansion_id": expansion_id})

    def mythic_plus_static_data(self, expansion_id: int) -> dict:
        """Season reference data. Used for the season cut-off: the date after which a kill no longer earns
        Cutting Edge / Ahead of the Curve, which is also when the Mythic+ season ends."""
        return self._get("/mythic-plus/static-data", {"expansion_id": expansion_id})

    def raid_rankings(
        self, raid: str, difficulty: str, region: str, realm: str | None = None, page: int = 0, limit: int = 100
    ) -> list[dict]:
        params: dict[str, Any] = {"raid": raid, "difficulty": difficulty, "region": region, "limit": limit, "page": page}
        if realm:
            params["realm"] = realm
        data = self._get("/raiding/raid-rankings", params)
        return data.get("raidRankings") or []

    def boss_kill(self, region: str, realm: str, guild: str, raid: str, boss: str, difficulty: str) -> dict:
        return self._get(
            "/guilds/boss-kill",
            {"region": region, "realm": realm, "guild": guild, "raid": raid, "boss": boss, "difficulty": difficulty},
        )
