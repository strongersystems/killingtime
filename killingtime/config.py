"""Application settings, loaded from environment variables and an optional .env file."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class GuildRef:
    """A guild identified by name, realm slug and region (all lower-cased for matching)."""

    name: str
    realm_slug: str
    region: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.name.lower(), self.realm_slug.lower(), self.region.lower())


def slugify_realm(realm: str) -> str:
    """Warcraft Logs / Raider.IO realm slug: lower-case, spaces and apostrophes removed."""
    return realm.strip().lower().replace("'", "").replace(" ", "-")


def parse_raid_teams(raw: str) -> dict[str, list[str]]:
    """Parse ``Team A: Player, Other; Team B: Someone`` into {team: [players]} (order preserved)."""
    out: dict[str, list[str]] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"Bad RAID_TEAMS entry {chunk!r}; expected 'Team name: Player, Player'")
        name, players = chunk.split(":", 1)
        roster = [p.strip() for p in players.split(",") if p.strip()]
        if not name.strip() or not roster:
            raise ValueError(f"Bad RAID_TEAMS entry {chunk!r}; expected 'Team name: Player, Player'")
        out[name.strip()] = roster
    return out


def parse_rival_guilds(raw: str) -> list[GuildRef]:
    """Parse ``Name@realm/region; Other Name@realm/region`` into GuildRefs."""
    out: list[GuildRef] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "@" not in chunk or "/" not in chunk.split("@", 1)[1]:
            raise ValueError(f"Bad RIVAL_GUILDS entry {chunk!r}; expected 'Name@realm/region'")
        name, rest = chunk.split("@", 1)
        realm, region = rest.rsplit("/", 1)
        out.append(GuildRef(name.strip(), slugify_realm(realm), region.strip().lower()))
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Warcraft Logs
    wcl_client_id: str = ""
    wcl_client_secret: str = ""
    wcl_token_url: str = "https://www.warcraftlogs.com/oauth/token"
    wcl_api_url: str = "https://www.warcraftlogs.com/api/v2/client"
    wcl_token_cache: str = ".wcl_token.json"

    # Raider.IO
    rio_api_url: str = "https://raider.io/api/v1"
    rio_realm_scan_pages: int = 2

    # Home guild
    guild_name: str = "Killing Time"
    guild_realm: str = "Draenor"
    guild_region: str = "EU"

    rival_guilds: str = ""
    sync_expansions: int = 2
    tier_map: str = ""  # JSON {"wcl_zone_id": "rio_raid_slug"}
    # Raid teams: "CE Team: Nórmán, Elelena; 6 Hour Team: Andrewro, Billadin". Reports are assigned to the team
    # with the most roster members in attendance; a report needs at least RAID_TEAM_MIN_MATCHES matches.
    raid_teams: str = ""
    raid_team_min_matches: int = 2
    # Alts, which no API exposes: "Findruid: Findpal, Finddk; Norman: Normanpriest". Shown on Meet the Team.
    raid_alts: str = ""
    # Where a member's generated portrait frames live. {slug} is the lower-case name, {n} the frame number (1-5).
    # Drop five images per player anywhere reachable and the Meet the Team card animates them on hover.
    member_image_url: str = "/static/members/{slug}/{n}.webp"
    # Parses: how many reports' rankings to fetch per sync (keeps the WCL points budget in check).
    sync_parses_per_run: int = 60

    # Ask (Claude)
    anthropic_api_key: str = ""
    ask_model: str = "claude-opus-5"
    ask_effort: str = "high"
    ask_enable_fallbacks: bool = True
    ask_max_tool_calls: int = 14

    # Storage / server
    kt_db_path: str = Field(default="data/killingtime.db")
    kt_host: str = "127.0.0.1"
    kt_port: int = 8000

    # Cloudflare Containers: snapshot persistence through the Worker, and auto-sync on an empty database.
    kt_state_url: str = ""
    kt_state_secret: str = ""
    kt_auto_sync: bool = False

    # Public guild site (rendered after every sync, served at /public and pushed to the Worker).
    site_url: str = ""  # e.g. https://killingtime.fyi (only used for links / canonical URL)
    site_tagline: str = ""
    site_about: str = ""
    site_raid_times: str = ""
    # The window the guild actually advertises, e.g. "21:00-24:00". When set it replaces the times worked out from
    # the logs: the median start of a pull is a few minutes after the raid begins, which is not what people mean
    # when they ask what time you raid.
    site_raid_hours: str = ""
    site_recruiting: str = ""  # e.g. "Recruiting: 1 healer, ranged DPS"
    site_apply_url: str = ""
    site_discord_url: str = ""

    @field_validator("ask_effort")
    @classmethod
    def _effort(cls, v: str) -> str:
        v = v.lower()
        if v not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("ASK_EFFORT must be one of low, medium, high, xhigh, max")
        return v

    @property
    def home_guild(self) -> GuildRef:
        return GuildRef(self.guild_name, slugify_realm(self.guild_realm), self.guild_region.lower())

    @property
    def rivals(self) -> list[GuildRef]:
        return parse_rival_guilds(self.rival_guilds)

    @property
    def teams(self) -> dict[str, list[str]]:
        return parse_raid_teams(self.raid_teams)

    @property
    def alts(self) -> dict[str, list[str]]:
        """{main: [alts]} from RAID_ALTS. No API links alts to a player, so this is filled in by hand."""
        return parse_raid_teams(self.raid_alts)

    @property
    def team_names(self) -> list[str]:
        return list(self.teams)

    @property
    def tier_map_dict(self) -> dict[int, str]:
        if not self.tier_map.strip():
            return {}
        raw = json.loads(self.tier_map)
        return {int(k): str(v) for k, v in raw.items()}

    @property
    def wcl_configured(self) -> bool:
        return bool(self.wcl_client_id and self.wcl_client_secret)

    @property
    def ask_configured(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
