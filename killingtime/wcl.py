"""Warcraft Logs API v2 client (OAuth2 client-credentials + GraphQL).

Docs: https://www.warcraftlogs.com/api/docs
Endpoints:
    POST https://www.warcraftlogs.com/oauth/token   (grant_type=client_credentials, basic auth)
    POST https://www.warcraftlogs.com/api/v2/client (GraphQL, Bearer token)

The client is deliberately small and synchronous: the sync job runs a few hundred
queries at most, and the public API budget is measured in "points per hour"
(see :meth:`WCLClient.rate_limit`).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class WCLError(RuntimeError):
    """Raised for authentication failures, GraphQL errors or exhausted rate limits."""


class WCLClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_url: str = "https://www.warcraftlogs.com/oauth/token",
        api_url: str = "https://www.warcraftlogs.com/api/v2/client",
        token_cache_path: str | None = ".wcl_token.json",
        http: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not client_id or not client_secret:
            raise WCLError("WCL_CLIENT_ID and WCL_CLIENT_SECRET must be set (see docs/SETUP.md)")
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.api_url = api_url
        self.token_cache_path = token_cache_path
        self._http = http or httpx.Client(timeout=timeout, headers={"User-Agent": "killingtime-progress/0.1"})
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self.queries_made = 0
        self._load_cached_token()

    # ------------------------------------------------------------------ auth
    def _load_cached_token(self) -> None:
        if not self.token_cache_path or not os.path.exists(self.token_cache_path):
            return
        try:
            with open(self.token_cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("client_id") == self.client_id and data.get("expires_at", 0) > time.time() + 60:
                self._token = data["access_token"]
                self._token_expiry = data["expires_at"]
        except (OSError, ValueError, KeyError):
            pass

    def _save_cached_token(self) -> None:
        if not self.token_cache_path:
            return
        try:
            with open(self.token_cache_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {"client_id": self.client_id, "access_token": self._token, "expires_at": self._token_expiry}, fh
                )
            os.chmod(self.token_cache_path, 0o600)
        except OSError:
            log.debug("could not cache WCL token", exc_info=True)

    def token(self, force: bool = False) -> str:
        """Return a valid bearer token, fetching a new one with client credentials if needed."""
        if self._token and not force and time.time() < self._token_expiry - 60:
            return self._token
        resp = self._http.post(
            self.token_url,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
        )
        if resp.status_code != 200:
            raise WCLError(
                f"Warcraft Logs token request failed ({resp.status_code}). "
                "Check WCL_CLIENT_ID / WCL_CLIENT_SECRET. Response: " + resp.text[:300]
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + float(payload.get("expires_in", 3600))
        self._save_cached_token()
        return self._token

    # --------------------------------------------------------------- GraphQL
    def query(self, gql: str, variables: dict[str, Any] | None = None, _retry: bool = True) -> dict[str, Any]:
        """Run a GraphQL query and return the ``data`` object. Raises WCLError on any error.

        A read timeout used to come out as httpx.ReadTimeout, which is not a WCLError, so it sailed past every
        caller's handling and took the whole sync down with it, publish and all. One slow response is not a reason
        to lose an hour of work: it is retried once and then reported as a WCLError like anything else."""
        try:
            resp = self._http.post(
                self.api_url,
                json={"query": gql, "variables": variables or {}},
                headers={"Authorization": f"Bearer {self.token()}"},
            )
        except httpx.HTTPError as exc:
            if not _retry:
                raise WCLError(f"Warcraft Logs request failed: {exc}") from exc
            time.sleep(2)
            return self.query(gql, variables, _retry=False)
        self.queries_made += 1
        if resp.status_code == 401 and _retry:
            self.token(force=True)
            return self.query(gql, variables, _retry=False)
        if resp.status_code == 429:
            raise WCLError(
                "Warcraft Logs rate limit reached (429). The points budget resets hourly; "
                "re-run the sync later. Consider fewer expansions (SYNC_EXPANSIONS) or an incremental sync."
            )
        if resp.status_code != 200:
            raise WCLError(f"Warcraft Logs API HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if body.get("errors"):
            msgs = "; ".join(e.get("message", str(e)) for e in body["errors"])
            # Partial data is still useful (e.g. an optional field failing) - surface it via the error.
            raise WCLError(f"GraphQL error: {msgs}")
        return body.get("data") or {}

    # -------------------------------------------------------------- queries
    def rate_limit(self) -> dict[str, Any]:
        data = self.query("{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }")
        return data["rateLimitData"]

    def expansions(self) -> list[dict[str, Any]]:
        data = self.query("{ worldData { expansions { id name } } }")
        return data["worldData"]["expansions"] or []

    def zones(self, expansion_id: int) -> list[dict[str, Any]]:
        gql = """
        query Zones($exp: Int) {
          worldData {
            zones(expansion_id: $exp) {
              id name frozen
              expansion { id name }
              difficulties { id name sizes }
              encounters { id name }
            }
          }
        }"""
        data = self.query(gql, {"exp": expansion_id})
        return data["worldData"]["zones"] or []

    def zone_partitions(self, expansion_id: int) -> dict[int, list[dict[str, Any]]]:
        """Patch partitions per zone, keyed by zone id.

        Kept apart from :meth:`zones` on purpose. A partition is what Warcraft Logs calls a patch within a tier,
        and rankings always belong to one; asking for the field alongside the zone sync would mean a schema change
        at their end could take the zones down with it, and zones are load-bearing.
        """
        gql = """
        query Partitions($exp: Int) {
          worldData { zones(expansion_id: $exp) { id partitions { id name compactName default } } }
        }"""
        data = self.query(gql, {"exp": expansion_id})
        out: dict[int, list[dict[str, Any]]] = {}
        for z in (data.get("worldData") or {}).get("zones") or []:
            parts = z.get("partitions") or []
            if parts:
                out[int(z["id"])] = parts
        return out

    def guild(self, name: str, server_slug: str, server_region: str) -> dict[str, Any] | None:
        gql = """
        query Guild($name: String!, $slug: String!, $region: String!) {
          guildData {
            guild(name: $name, serverSlug: $slug, serverRegion: $region) {
              id name
              faction { name }
              server { name slug region { compactName slug } }
            }
          }
        }"""
        data = self.query(gql, {"name": name, "slug": server_slug, "region": server_region})
        return data["guildData"]["guild"]

    def reports(
        self,
        guild_id: int,
        zone_id: int | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        page: int = 1,
        limit: int = 100,
    ) -> dict[str, Any]:
        gql = """
        query Reports($guildID: Int!, $zoneID: Int, $start: Float, $end: Float, $page: Int!, $limit: Int!) {
          reportData {
            reports(guildID: $guildID, zoneID: $zoneID, startTime: $start, endTime: $end, page: $page, limit: $limit) {
              has_more_pages current_page last_page total
              data { code title startTime endTime zone { id name } owner { name } }
            }
          }
        }"""
        data = self.query(
            gql,
            {"guildID": guild_id, "zoneID": zone_id, "start": start_time, "end": end_time, "page": page, "limit": limit},
        )
        return data["reportData"]["reports"]

    def all_reports(self, guild_id: int, start_time: float | None = None, zone_id: int | None = None) -> list[dict]:
        """Iterate every page of reports for a guild (newest first from the API)."""
        out: list[dict] = []
        page = 1
        while True:
            chunk = self.reports(guild_id, zone_id=zone_id, start_time=start_time, page=page)
            out.extend(chunk.get("data") or [])
            if not chunk.get("has_more_pages"):
                break
            page += 1
            if page > 200:  # safety valve
                break
        return out

    FIGHT_FIELDS = (
        "id encounterID name difficulty kill startTime endTime bossPercentage fightPercentage "
        "lastPhase size averageItemLevel"
    )

    def report_fights(self, codes: list[str], batch: int = 8) -> dict[str, list[dict[str, Any]]]:
        """Fetch boss fights for many reports, several reports per request via GraphQL aliases."""
        result: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(codes), batch):
            group = codes[i : i + batch]
            parts = [
                f'r{j}: report(code: "{code}") {{ code fights(killType: Encounters) {{ {self.FIGHT_FIELDS} }} }}'
                for j, code in enumerate(group)
            ]
            gql = "query Fights { reportData { " + " ".join(parts) + " } }"
            data = self.query(gql)
            for j, code in enumerate(group):
                rep = (data.get("reportData") or {}).get(f"r{j}")
                result[code] = (rep or {}).get("fights") or []
        return result

    def guild_zone_ranking(self, guild_id: int, zone_id: int, difficulty: int = 5) -> dict[str, Any]:
        gql = """
        query ZoneRank($guildID: Int!, $zoneID: Int!, $difficulty: Int!) {
          guildData {
            guild(id: $guildID) {
              zoneRanking(zoneId: $zoneID) {
                progress { worldRank { number percentile } regionRank { number percentile } serverRank { number percentile } }
                speed(difficulty: $difficulty) { worldRank { number percentile } regionRank { number percentile } serverRank { number percentile } }
                completeRaidSpeed(difficulty: $difficulty) { worldRank { number percentile } regionRank { number percentile } serverRank { number percentile } }
              }
            }
          }
        }"""
        data = self.query(gql, {"guildID": guild_id, "zoneID": zone_id, "difficulty": difficulty})
        return ((data.get("guildData") or {}).get("guild") or {}).get("zoneRanking") or {}

    def report_rankings(self, code: str, metric: str = "dps") -> list[dict]:
        """Per-fight player rankings (parses) for a report. Only kills have rankings.

        Each entry: {fightID, encounter {id name}, difficulty, kill, roles: {tanks|healers|dps: {characters: [
        {name, class, spec, amount, rankPercent, bracketPercent, server {...}}]}}}.
        """
        gql = """
        query Rankings($code: String!, $metric: ReportRankingMetricType!) {
          reportData { report(code: $code) { rankings(playerMetric: $metric, compare: Parses) } }
        }"""
        data = self.query(gql, {"code": code, "metric": metric})
        rankings = ((data.get("reportData") or {}).get("report") or {}).get("rankings") or {}
        if isinstance(rankings, str):  # the field is a JSON scalar; some gateways return it serialised
            import json

            rankings = json.loads(rankings)
        return rankings.get("data") or []

    def guild_attendance(self, guild_id: int, zone_id: int | None = None, page: int = 1, limit: int = 25) -> dict:
        gql = """
        query Attendance($guildID: Int!, $zoneID: Int, $page: Int!, $limit: Int!) {
          guildData {
            guild(id: $guildID) {
              attendance(zoneID: $zoneID, page: $page, limit: $limit) {
                has_more_pages current_page
                data { code startTime zone { id } players { name type presence } }
              }
            }
          }
        }"""
        data = self.query(gql, {"guildID": guild_id, "zoneID": zone_id, "page": page, "limit": limit})
        return ((data.get("guildData") or {}).get("guild") or {}).get("attendance") or {}

    def all_attendance(self, guild_id: int, zone_id: int | None = None, max_pages: int = 20) -> list[dict]:
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            chunk = self.guild_attendance(guild_id, zone_id=zone_id, page=page)
            out.extend(chunk.get("data") or [])
            if not chunk.get("has_more_pages"):
                break
            page += 1
        return out
