"""Natural-language questions about guild progress, answered by Claude with read-only SQL tools.

Flow: question -> Claude (with the database schema in a cached system prompt) -> zero or more
tool calls (``run_sql``, ``get_overview``, ``render_chart``) -> markdown answer plus chart specs
that the web UI renders with Chart.js. The SQL tool is read-only by construction: it runs on a
separate ``mode=ro`` connection with an authorizer that only permits SELECT/READ.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError, model_validator

from . import metrics
from .config import Settings
from .db import schema_description

log = logging.getLogger(__name__)

MAX_ROWS = 200
SQL_TIMEOUT_S = 8.0


# ------------------------------------------------------------------------------------ chart spec
class Series(BaseModel):
    name: str = Field(max_length=60)
    data: list[float | None]


class ChartSpec(BaseModel):
    """A small, renderer-neutral chart description. Kept deliberately simple so it is hard to misuse."""

    title: str = Field(max_length=120)
    type: Literal["line", "bar", "hbar", "stacked"] = "bar"
    categories: list[str] = Field(description="x-axis labels (or y-axis labels for hbar)")
    series: list[Series] = Field(min_length=1, max_length=8)
    x_label: str = ""
    y_label: str = ""
    note: str = Field(default="", max_length=240, description="Optional caption shown under the chart")

    @model_validator(mode="after")
    def _lengths(self) -> ChartSpec:
        n = len(self.categories)
        for s in self.series:
            if len(s.data) != n:
                raise ValueError(f"series '{s.name}' has {len(s.data)} values but there are {n} categories")
        if n == 0 or n > 400:
            raise ValueError("categories must have between 1 and 400 entries")
        return self


@dataclass
class AskResult:
    question: str
    answer: str
    charts: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    stop_reason: str = ""
    elapsed_s: float = 0.0


# ------------------------------------------------------------------------------------ SQL guard
_ALLOWED_AUTH = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


def _authorizer(action: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_AUTH else sqlite3.SQLITE_DENY


def open_readonly(db_path: str) -> sqlite3.Connection:
    """A dedicated read-only connection for the SQL tool."""
    if db_path == ":memory:":
        raise ValueError("Ask needs a file-backed database (KT_DB_PATH), not :memory:")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.set_authorizer(_authorizer)
    return conn


def safe_select(conn: sqlite3.Connection, sql: str, max_rows: int = MAX_ROWS) -> dict[str, Any]:
    """Run a single SELECT/WITH statement with a row cap and a time budget; raise ValueError otherwise."""
    stmt = sql.strip().rstrip(";").strip()
    if not stmt:
        raise ValueError("empty SQL")
    if ";" in stmt:
        raise ValueError("only one statement per call")
    head = stmt.lstrip("(").split(None, 1)[0].upper()
    if head not in {"SELECT", "WITH"}:
        raise ValueError("only SELECT / WITH queries are allowed")
    deadline = time.monotonic() + SQL_TIMEOUT_S
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 5000)
    try:
        cur = conn.execute(stmt)
        rows = cur.fetchmany(max_rows + 1)
        columns = [d[0] for d in cur.description] if cur.description else []
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise ValueError(f"query exceeded {SQL_TIMEOUT_S:.0f}s time budget; add filters or LIMIT") from exc
        raise ValueError(f"SQL error: {exc}") from exc
    except sqlite3.DatabaseError as exc:  # includes authorizer denials ("not authorized")
        raise ValueError(f"SQL rejected: {exc}") from exc
    finally:
        conn.set_progress_handler(None, 0)
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


# ------------------------------------------------------------------------------------ tools
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_overview",
        "description": (
            "Current snapshot: home guild, tiers we have logs for (zone ids, kill counts per difficulty), "
            "Raider.IO summaries and ranks, and the last sync time. Call this first when you need zone ids "
            "or want to know what data exists."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "run_sql",
        "description": (
            "Run one read-only SQLite SELECT (or WITH ... SELECT) against the guild database and get rows back. "
            "Max 200 rows per call - aggregate in SQL rather than pulling raw rows. Timestamps are ms since epoch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT statement."}},
            "required": ["sql"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "render_chart",
        "description": (
            "Queue a chart to display with your answer. Use for trends over time (line), comparisons across "
            "bosses/guilds/tiers (bar or hbar), or composition (stacked). Keep it to at most 8 series; every "
            "series must have one value per category (use null for missing). Numbers only - no formatted strings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "type": {"type": "string", "enum": ["line", "bar", "hbar", "stacked"]},
                "categories": {"type": "array", "items": {"type": "string"}},
                "series": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "data": {"type": "array", "items": {"type": ["number", "null"]}},
                        },
                        "required": ["name", "data"],
                        "additionalProperties": False,
                    },
                },
                "x_label": {"type": "string"},
                "y_label": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["title", "type", "categories", "series", "x_label", "y_label", "note"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def build_system_prompt(conn: sqlite3.Connection, settings: Settings) -> str:
    """Stable system prompt (no timestamps or volatile data) so it can be cached across questions."""
    return f"""You are the raid analyst for the World of Warcraft guild "{settings.guild_name}" on {settings.guild_realm} ({settings.guild_region.upper()}).
You answer questions about the guild's raid progression using a SQLite database that is synced from
Warcraft Logs (our own pulls, kills, attendance, rankings) and Raider.IO (per-boss progress and ranks for every
guild on the realm, plus configured rival guilds).

# Database schema
{schema_description(conn)}

# How to work
- Use run_sql for anything numeric. Aggregate in SQL; never guess numbers. If a query returns nothing, say so and check
  which zones/difficulties actually have data (get_overview) before concluding.
- "The current tier" is the newest zone in get_overview's tiers list. "Previous tier" is the next one down.
- Mythic progression is difficulty 5, Heroic 4, Normal 3. Default to the highest difficulty the guild has pulled unless
  the user says otherwise, and state which difficulty you used.
- Our own pull-level data (v_pulls, v_first_kills, v_raid_nights) only exists for the home guild (guilds.is_home = 1).
  For other guilds use v_rio_progress / rio_rankings (Raider.IO): first_defeated, num_pulls, best_percent.
- pulls_to_kill counts pulls up to and including the first kill; wipes_before_kill = pulls_to_kill - 1 for killed bosses.
- Comparing tiers: use days since the guild's first pull in that tier/difficulty, or cumulative pulls by boss order,
  not calendar dates, so tiers line up.
- Attendance: presence = 1 means present. Attendance % = raids attended / total raids in that zone.
- When a comparison or trend is the heart of the answer, call render_chart (at most 2 charts per answer).
  Categories are labels; series values must be numbers. Prefer one chart that answers the question over many.
- Round sensibly (pulls as integers, percentages to 1 decimal, hours to 1 decimal).

# Answer style
- Lead with the direct answer in one or two sentences, then the supporting numbers in a compact markdown table.
- Mention the data source (our logs vs Raider.IO) when it matters, and note gaps (e.g. a rival hides pull counts).
- Do not describe your SQL or tool calls unless asked; the UI shows them separately.
- Keep it concise. No closing offers.
"""


class Asker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        client: anthropic.Anthropic | None = None,
        ro_conn: sqlite3.Connection | None = None,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key or None)
        self.ro = ro_conn or open_readonly(settings.kt_db_path)
        self.system_prompt = build_system_prompt(conn, settings)

    # ---------------------------------------------------------------- tool execution
    def run_tool(self, name: str, tool_input: dict[str, Any], charts: list[dict[str, Any]]) -> tuple[str, bool]:
        try:
            if name == "get_overview":
                ov = metrics.overview(self.conn)
                ov["difficulty_codes"] = {"3": "Normal", "4": "Heroic", "5": "Mythic"}
                return json.dumps(ov, default=str), False
            if name == "run_sql":
                res = safe_select(self.ro, str(tool_input.get("sql", "")))
                return json.dumps(res, default=str), False
            if name == "render_chart":
                spec = ChartSpec.model_validate(tool_input)
                charts.append(spec.model_dump())
                return f"chart #{len(charts)} queued: {spec.title}", False
            return f"unknown tool {name}", True
        except (ValueError, ValidationError) as exc:
            return f"Error: {exc}", True

    # ---------------------------------------------------------------- Claude call
    def _create(self, messages: list[dict[str, Any]]) -> Any:
        kwargs: dict[str, Any] = dict(
            model=self.settings.ask_model,
            max_tokens=16000,
            system=[{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
            output_config={"effort": self.settings.ask_effort},
        )
        if self.settings.ask_enable_fallbacks:
            return self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        return self.client.messages.create(**kwargs)

    def ask(self, question: str, history: list[dict[str, str]] | None = None) -> AskResult:
        """Answer ``question``. ``history`` is a list of {"question", "answer"} from earlier turns."""
        started = time.monotonic()
        messages: list[dict[str, Any]] = []
        for turn in history or []:
            if turn.get("question") and turn.get("answer"):
                messages.append({"role": "user", "content": turn["question"]})
                messages.append({"role": "assistant", "content": turn["answer"]})
        messages.append({"role": "user", "content": question})

        charts: list[dict[str, Any]] = []
        trace: list[dict[str, Any]] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        answer_parts: list[str] = []
        stop_reason = ""
        model = self.settings.ask_model
        tool_calls = 0
        while True:
            response = self._create(messages)
            model = getattr(response, "model", model)
            u = response.usage
            for k in usage:
                usage[k] += int(getattr(u, k, 0) or 0)
            stop_reason = response.stop_reason or ""
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    answer_parts.append(block.text)
            if stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                why = getattr(details, "explanation", None) or "the model declined this request"
                answer_parts.append(f"_The model declined to answer: {why}_")
                break
            if stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            if stop_reason != "tool_use":
                if stop_reason == "max_tokens":
                    answer_parts.append("_(answer truncated - ask a narrower question)_")
                break
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for tu in tool_uses:
                tool_calls += 1
                if tool_calls > self.settings.ask_max_tool_calls:
                    out, err = "Error: tool call budget exhausted - answer with what you have.", True
                else:
                    out, err = self.run_tool(tu.name, dict(tu.input), charts)
                trace.append(
                    {
                        "tool": tu.name,
                        "input": tu.input,
                        "output_preview": out[:600],
                        "error": err,
                    }
                )
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out, "is_error": err})
            messages.append({"role": "user", "content": results})
            if tool_calls > self.settings.ask_max_tool_calls + len(tool_uses):
                break

        # Text emitted alongside earlier tool calls is usually preamble; keep the last substantive block(s).
        answer = "\n\n".join(answer_parts).strip() or "_No answer was produced._"
        return AskResult(
            question=question,
            answer=answer,
            charts=charts,
            trace=trace,
            usage=usage,
            model=model,
            stop_reason=stop_reason,
            elapsed_s=round(time.monotonic() - started, 1),
        )


def describe_error(exc: Exception) -> str:
    """Turn an SDK exception into a one-line, user-facing message."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "Anthropic API key rejected. Check ANTHROPIC_API_KEY in .env."
    if isinstance(exc, anthropic.NotFoundError):
        return "Model not found. Check ASK_MODEL (default claude-opus-5)."
    if isinstance(exc, anthropic.RateLimitError):
        return "Anthropic rate limit hit. Wait a moment and try again."
    if isinstance(exc, anthropic.BadRequestError):
        return f"Bad request to the Claude API: {exc.message}"
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude API error {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the Claude API (network error)."
    return f"{type(exc).__name__}: {exc}"
