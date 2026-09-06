"""Ask feature: SQL guard, chart validation, and the tool loop with a scripted fake Claude client."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from killingtime.ask import TOOLS, Asker, ChartSpec, build_system_prompt, open_readonly, safe_select


def test_safe_select_guards(synced):
    conn, *_ , settings = synced
    ro = open_readonly(settings.kt_db_path)
    res = safe_select(ro, "SELECT encounter_name, pulls_to_kill FROM v_first_kills WHERE zone_id = 44 ORDER BY encounter_ord")
    assert res["columns"] == ["encounter_name", "pulls_to_kill"] and res["row_count"] == 3
    for bad in ["DELETE FROM fights", "UPDATE guilds SET name='x'", "PRAGMA journal_mode=DELETE",
                "SELECT 1; DROP TABLE fights", "ATTACH DATABASE 'x' AS y", "INSERT INTO meta VALUES ('a','b')"]:
        with pytest.raises(ValueError):
            safe_select(ro, bad)
    # CTEs are fine, row cap applies
    res = safe_select(ro, "WITH x AS (SELECT * FROM v_pulls) SELECT * FROM x", max_rows=5)
    assert res["row_count"] == 5 and res["truncated"] is True
    # the data is still intact
    assert conn.execute("SELECT COUNT(*) FROM fights").fetchone()[0] > 0


def test_chart_spec_validation():
    ok = ChartSpec(title="t", type="bar", categories=["a", "b"], series=[{"name": "s", "data": [1, None]}])
    assert ok.series[0].data == [1, None]
    with pytest.raises(ValueError):
        ChartSpec(title="t", type="bar", categories=["a"], series=[{"name": "s", "data": [1, 2]}])
    with pytest.raises(ValueError):
        ChartSpec(title="t", type="pie", categories=["a"], series=[{"name": "s", "data": [1]}])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ChartSpec(title="t", type="bar", categories=["a"], series=[{"name": f"s{i}", "data": [1]} for i in range(9)])


def test_system_prompt_is_stable(synced):
    conn, *_, settings = synced
    p1, p2 = build_system_prompt(conn, settings), build_system_prompt(conn, settings)
    assert p1 == p2 and "v_first_kills" in p1 and "Killing Time" in p1
    assert {t["name"] for t in TOOLS} == {"get_overview", "run_sql", "render_chart"}


class Block(SimpleNamespace):
    pass


class FakeMessages:
    """Scripted responses: first call asks for two tools, second call answers."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        # snapshot: the Asker mutates its messages list between calls
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.script.pop(0)


def msg(content, stop_reason, model="claude-opus-5"):
    return SimpleNamespace(content=content, stop_reason=stop_reason, model=model, stop_details=None,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=50, cache_creation_input_tokens=0))


def test_ask_loop_runs_tools_and_collects_charts(synced):
    conn, *_, settings = synced
    settings = settings.model_copy(update={"ask_enable_fallbacks": False})
    script = [
        msg([Block(type="text", text="Let me check."),
             Block(type="tool_use", id="t1", name="get_overview", input={}),
             Block(type="tool_use", id="t2", name="run_sql", input={"sql": "SELECT encounter_name, pulls_to_kill FROM v_first_kills WHERE zone_id = 44 AND difficulty = 5 ORDER BY encounter_ord"}),
             Block(type="tool_use", id="t3", name="run_sql", input={"sql": "DROP TABLE fights"})], "tool_use"),
        msg([Block(type="tool_use", id="t4", name="render_chart", input={
            "title": "Pulls to kill", "type": "bar", "categories": ["Plexus Sentinel", "Loom'ithar", "Dimensius"],
            "series": [{"name": "Pulls", "data": [4, 6, 10]}], "x_label": "", "y_label": "pulls", "note": ""})], "tool_use"),
        msg([Block(type="text", text="Plexus 4, Loom 6, Dimensius 10 pulls.")], "end_turn"),
    ]
    fake = FakeMessages(script)
    client = SimpleNamespace(messages=fake, beta=SimpleNamespace(messages=fake))
    asker = Asker(conn, settings, client=client)
    res = asker.ask("How many pulls per boss last tier?", history=[{"question": "hi", "answer": "hello"}])
    assert "Dimensius 10" in res.answer
    assert len(res.charts) == 1 and res.charts[0]["series"][0]["data"] == [4, 6, 10]
    assert [t["tool"] for t in res.trace] == ["get_overview", "run_sql", "run_sql", "render_chart"]
    assert res.trace[2]["error"] is True and "only SELECT" in res.trace[2]["output_preview"]
    sql_out = json.loads(res.trace[1]["output_preview"]) if len(res.trace[1]["output_preview"]) < 600 else None
    assert sql_out is None or sql_out["rows"][0] == ["Plexus Sentinel", 4]
    assert res.usage["input_tokens"] == 300 and res.usage["cache_read_input_tokens"] == 150
    # request shape: cached system prompt, effort, tools, history preserved, tool results returned together
    first = fake.calls[0]
    assert first["model"] == "claude-opus-5"
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["output_config"] == {"effort": "high"}
    assert first["messages"][0] == {"role": "user", "content": "hi"}
    second = fake.calls[1]
    results = second["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2", "t3"] and results[2]["is_error"] is True
    assert "fallbacks" not in first


def test_ask_uses_fallbacks_when_enabled(synced):
    conn, *_, settings = synced
    fake = FakeMessages([msg([Block(type="text", text="ok")], "end_turn")])
    client = SimpleNamespace(messages=None, beta=SimpleNamespace(messages=fake))
    Asker(conn, settings, client=client).ask("q")
    call = fake.calls[0]
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]


def test_ask_refusal_is_reported(synced):
    conn, *_, settings = synced
    m = msg([], "refusal")
    m.stop_details = SimpleNamespace(category="other", explanation="not appropriate")
    fake = FakeMessages([m])
    client = SimpleNamespace(messages=None, beta=SimpleNamespace(messages=fake))
    res = Asker(conn, settings, client=client).ask("q")
    assert "declined" in res.answer and "not appropriate" in res.answer
