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
