"""Tests for reading the OpenCode v2 schema (session_v2 / session_message)."""

import json
import sqlite3
from pathlib import Path

import pytest

from ocmonitor.utils.sqlite_utils import SQLiteProcessor


def _assistant(model_id, provider_id, tokens_in, tools=(), created=1000, cost=0.0):
    return json.dumps({
        "time": {"created": created, "completed": created + 500},
        "agent": "build",
        "model": {"id": model_id, "providerID": provider_id, "variant": "default"},
        "content": [{"type": "text", "text": "hi"}] + [
            {"type": "tool", "name": name, "state": {"status": status}}
            for name, status in tools
        ],
        "cost": cost,
        "tokens": {"input": tokens_in, "output": 10, "reasoning": 0,
                   "cache": {"read": 5, "write": 0}},
        "finish": "stop",
    })


@pytest.fixture
def v2_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT);
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
            directory TEXT NOT NULL, title TEXT,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
        );
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
            seq INTEGER NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL
        );
    """)
    conn.execute("INSERT INTO project VALUES ('p1', '/work/myproj', 'myproj')")
    conn.execute(
        "INSERT INTO session_v2 VALUES ('ses_a', 'p1', NULL, '/work/myproj', 'Main', 1000, 2000)"
    )
    conn.execute(
        "INSERT INTO session_v2 VALUES ('ses_b', 'p1', 'ses_a', '/work/myproj', NULL, 1500, 2500)"
    )
    rows = [
        ("m1", "ses_a", "user", 1, json.dumps({"text": "hello"})),
        ("m2", "ses_a", "assistant", 2, _assistant(
            "GPT-5.5", "OpenAI", 100,
            tools=[("shell", "completed"), ("shell", "error"), ("read", "running")],
            cost=0.25,
        )),
        ("m3", "ses_a", "assistant", 3, _assistant(
            "claude-sonnet-5", "anthropic", 200, tools=[("read", "completed")]
        )),
        ("m4", "ses_a", "assistant", 4, "not json"),
        ("m5", "ses_b", "assistant", 1, _assistant("gpt-5.5", "openai", 50)),
    ]
    for mid, sid, typ, seq, data in rows:
        conn.execute(
            "INSERT INTO session_message VALUES (?, ?, ?, ?, 1000, 1000, ?)",
            (mid, sid, typ, seq, data),
        )
    conn.commit()
    conn.close()
    return db_path


def test_load_all_sessions_reads_v2_tables(v2_db: Path):
    sessions = {s.session_id: s for s in SQLiteProcessor.load_all_sessions(v2_db)}

    assert set(sessions) == {"ses_a", "ses_b"}
    main = sessions["ses_a"]
    assert main.session_title == "Main"
    assert len(main.files) == 2
    first = main.files[0]
    assert first.model_id == "gpt-5.5"
    assert first.provider_id == "openai"
    assert first.tokens.input == 100
    assert first.tokens.cache_read == 5
    assert first.agent == "build"
    # v2 messages have no path; project comes from session directory
    assert first.project_path == "/work/myproj"
    assert main.project_name == "myproj"
    assert sessions["ses_b"].parent_id == "ses_a"


def test_tool_usage_reads_inline_content(v2_db: Path):
    stats = {s.tool_name: s for s in
             SQLiteProcessor.load_tool_usage_for_sessions(["ses_a"], v2_db)}

    assert set(stats) == {"shell", "read"}
    assert stats["shell"].success_count == 1
    assert stats["shell"].failure_count == 1
    assert stats["read"].total_calls == 1  # running call excluded


def test_tool_usage_by_model_v2(v2_db: Path):
    result = {m.model_name: m for m in
              SQLiteProcessor.load_tool_usage_by_model_for_sessions(["ses_a"], v2_db)}

    assert set(result) == {"GPT-5.5", "claude-sonnet-5"}
    assert result["GPT-5.5"].total_calls == 2


def test_model_queries_v2(v2_db: Path):
    assert SQLiteProcessor.find_matching_models("sonnet", v2_db) == ["claude-sonnet-5"]

    # "GPT-5.5" and "gpt-5.5" are one model
    assert SQLiteProcessor.find_matching_models("gpt", v2_db) == ["gpt-5.5"]
    stats = SQLiteProcessor.get_model_detail_stats("GPT-5.5", {}, v2_db)
    assert stats.total_interactions == 2
    assert stats.total_tokens.input == 150


def test_database_stats_v2(v2_db: Path):
    stats = SQLiteProcessor.get_database_stats(v2_db)

    assert stats["session_count"] == 2
    assert stats["message_count"] == 5
    assert stats["sub_agent_count"] == 1


def test_live_workflows_keep_sessions_without_token_usage(tmp_path: Path):
    """Providers that report no usage still show in live, but not in reports."""
    import time

    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT);
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
            directory TEXT NOT NULL, title TEXT,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
        );
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
            seq INTEGER NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL
        );
    """)
    now = int(time.time() * 1000)
    conn.execute("INSERT INTO project VALUES ('p1', '/work/proj', 'proj')")
    conn.execute(
        "INSERT INTO session_v2 VALUES ('ses_z', 'p1', NULL, '/work/proj', 'No usage', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO session_message VALUES ('m1', 'ses_z', 'assistant', 1, ?, ?, ?)",
        (now, now, json.dumps({
            "time": {"created": now, "completed": now + 500},
            "model": {"id": "router", "providerID": "custom"},
            "content": [{"type": "text", "text": "hi"}],
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0,
                       "cache": {"read": 0, "write": 0}},
        })),
    )
    conn.commit()
    conn.close()

    workflow = SQLiteProcessor.get_most_recent_workflow(db_path)
    assert workflow["main_session"].session_id == "ses_z"
    assert len(SQLiteProcessor.get_all_active_workflows(db_path)) == 1
    assert SQLiteProcessor.load_all_sessions(db_path) == []


def _v1_assistant(model_id, tokens_in, created=1000):
    return json.dumps({
        "role": "assistant",
        "modelID": model_id,
        "providerID": "anthropic",
        "time": {"created": created, "completed": created + 500},
        "path": {"cwd": "/work/myproj"},
        "cost": 0.0,
        "tokens": {"input": tokens_in, "output": 10, "reasoning": 0,
                   "cache": {"read": 0, "write": 0}},
    })


@pytest.fixture
def upgraded_db(tmp_path: Path) -> Path:
    """A v2 database that still has the frozen v1 tables.

    ses_old was migrated without its messages, so they exist only in v1.
    ses_new has m1 in both (v2 copy has a rewritten completed time and the
    same tool call inline) and m2 only in v2.
    """
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT);
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
            directory TEXT NOT NULL, title TEXT,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
            directory TEXT NOT NULL, title TEXT,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
        );
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
            seq INTEGER NOT NULL, time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL, data TEXT NOT NULL
        );
    """)
    conn.execute("INSERT INTO project VALUES ('p1', '/work/myproj', 'myproj')")
    for table in ("session", "session_v2"):
        conn.execute(
            f"INSERT INTO {table} VALUES ('ses_old', 'p1', NULL, '/work/myproj', 'Old', 1000, 2000)"
        )
        conn.execute(
            f"INSERT INTO {table} VALUES ('ses_new', 'p1', NULL, '/work/myproj', 'New', 3000, 4000)"
        )

    conn.execute(
        "INSERT INTO message VALUES ('o1', 'ses_old', 1000, 1000, ?)",
        (_v1_assistant("claude-sonnet-4", 100, created=1000),),
    )
    conn.execute(
        "INSERT INTO message VALUES ('m1', 'ses_new', 3000, 3000, ?)",
        (_v1_assistant("claude-sonnet-4", 200, created=3000),),
    )
    for pid, mid, sid, tool in (("t1", "o1", "ses_old", "edit"), ("t2", "m1", "ses_new", "shell")):
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, 1000, 1000, ?)",
            (pid, mid, sid, json.dumps(
                {"type": "tool", "tool": tool, "state": {"status": "completed"}}
            )),
        )

    migrated = json.loads(_assistant(
        "claude-sonnet-4", "anthropic", 200, tools=[("shell", "completed")], created=3000
    ))
    migrated["time"]["completed"] = 9_999_999
    conn.execute(
        "INSERT INTO session_message VALUES ('m1', 'ses_new', 'assistant', 1, 3000, 3000, ?)",
        (json.dumps(migrated),),
    )
    conn.execute(
        "INSERT INTO session_message VALUES ('m2', 'ses_new', 'assistant', 2, 3500, 3500, ?)",
        (_assistant("gpt-5.5", "openai", 50, tools=[("read", "completed")], created=3500),),
    )
    conn.commit()
    conn.close()
    return db_path


def test_upgraded_db_keeps_v1_only_sessions(upgraded_db: Path):
    sessions = {s.session_id: s for s in SQLiteProcessor.load_all_sessions(upgraded_db)}

    assert set(sessions) == {"ses_old", "ses_new"}
    assert [f.tokens.input for f in sessions["ses_old"].files] == [100]
    assert [f.tokens.input for f in sessions["ses_new"].files] == [200, 50]


def test_upgraded_db_prefers_v1_copy_of_migrated_messages(upgraded_db: Path):
    sessions = {s.session_id: s for s in SQLiteProcessor.load_all_sessions(upgraded_db)}

    migrated = sessions["ses_new"].files[0]
    assert migrated.time_data.completed == 3500


def test_upgraded_db_counts_each_tool_call_once(upgraded_db: Path):
    stats = {s.tool_name: s.total_calls for s in
             SQLiteProcessor.load_tool_usage_for_sessions(["ses_old", "ses_new"], upgraded_db)}

    assert stats == {"edit": 1, "shell": 1, "read": 1}

    by_model = {m.model_name: m.total_calls for m in
                SQLiteProcessor.load_tool_usage_by_model_for_sessions(["ses_new"], upgraded_db)}
    assert by_model == {"claude-sonnet-4": 1, "gpt-5.5": 1}


def test_upgraded_db_model_queries_and_stats(upgraded_db: Path):
    assert SQLiteProcessor.find_matching_models("", upgraded_db) == ["claude-sonnet-4", "gpt-5.5"]

    stats = SQLiteProcessor.get_model_detail_stats("claude-sonnet-4", {}, upgraded_db)
    assert stats.total_interactions == 2
    assert stats.total_tokens.input == 300

    db_stats = SQLiteProcessor.get_database_stats(upgraded_db)
    assert db_stats["session_count"] == 2
    assert db_stats["message_count"] == 3
