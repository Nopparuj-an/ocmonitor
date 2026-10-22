"""SQLite database utilities for OpenCode v1.2.0+ session storage.

Supports both the v1 schema (``session``/``message``/``part`` tables) and the
OpenCode v2 schema (``session_v2``/``session_message`` tables, where tool calls
live inline in each assistant message's ``content`` array).
"""

import json
import sqlite3
import statistics
import time
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Any, Generator
from datetime import datetime

from ..utils.file_utils import FileProcessor
from ..models.session import SessionData, InteractionFile, TokenUsage, TimeData
from ..models.tool_usage import ToolUsageStats, ToolUsageSummary, ModelToolUsage
from ..models.analytics import ModelDetailStats


# SQL fragments per schema version. Queries alias the message table as ``m``
# and tool parts as ``p``.
_SCHEMA_V1: Dict[str, str] = {
    "session": "session",
    "message": "message",
    "is_assistant": "json_extract(m.data, '$.role') = 'assistant'",
    "model": "COALESCE(json_extract(m.data, '$.modelID'), json_extract(m.data, '$.model.modelID'))",
    "tool_from": "part p",
    "tool_from_with_message": "part p JOIN message m ON p.message_id = m.id",
    "tool_session": "p.session_id",
    "tool_data": "p.data",
    "tool_name_path": "$.tool",
}

_V2_TOOL_FROM = (
    "session_message m, json_each("
    "CASE WHEN json_valid(m.data) THEN m.data ELSE '{}' END, '$.content') p"
)

_SCHEMA_V2: Dict[str, str] = {
    "session": "session_v2",
    "message": "session_message",
    "is_assistant": "m.type = 'assistant'",
    "model": "json_extract(m.data, '$.model.id')",
    "tool_from": _V2_TOOL_FROM,
    "tool_from_with_message": _V2_TOOL_FROM,
    "tool_session": "m.session_id",
    "tool_data": "p.value",
    "tool_name_path": "$.name",
}


class SQLiteProcessor:
    """Handles SQLite database queries for OpenCode v1.2.0+ session storage."""

    DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

    @staticmethod
    def find_database_path(custom_path: Optional[Path] = None) -> Optional[Path]:
        """Find the OpenCode SQLite database.

        Args:
            custom_path: Optional custom path to check first

        Returns:
            Path to database file if found, None otherwise
        """
        import os

        candidates: List[Path] = []

        # 1) Explicit custom path (highest priority)
        if custom_path:
            candidates.append(Path(custom_path))

        # 2) Environment override
        env_db_path = os.environ.get("OCMONITOR_DATABASE_FILE")
        if env_db_path:
            candidates.append(Path(os.path.expanduser(os.path.expandvars(env_db_path))))

        # 3) Config path (if available)
        try:
            from ..config import config_manager

            configured_db_path = config_manager.config.paths.database_file
            if configured_db_path:
                candidates.append(
                    Path(os.path.expanduser(os.path.expandvars(configured_db_path)))
                )
        except Exception:
            # Config is optional for path discovery; continue with defaults
            pass

        # 4) Built-in platform defaults
        candidates.append(SQLiteProcessor.DEFAULT_DB_PATH)

        if os.name == "nt":
            candidates.append(
                Path(os.environ.get("APPDATA", "")) / "opencode" / "opencode.db"
            )

        for candidate in candidates:
            if candidate.exists():
                return candidate

        return None

    @staticmethod
    def _get_connection(db_path: Path) -> sqlite3.Connection:
        """Create a database connection with proper settings."""
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _get_schema(conn: sqlite3.Connection) -> Dict[str, str]:
        """Return SQL fragments for the schema version of this database.

        OpenCode v2 migrates all sessions into ``session_v2`` but leaves the
        legacy tables in place, so the presence of ``session_v2`` decides.
        """
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_v2'"
        ).fetchone()
        return _SCHEMA_V2 if row else _SCHEMA_V1

    @staticmethod
    def _extract_model_name(model_data: Any) -> str:
        """Extract model name from various possible locations in message data."""
        if isinstance(model_data, dict):
            # Try modelID directly
            if "modelID" in model_data:
                return model_data["modelID"] or "unknown"
            # Try nested model.modelID (v1) or model.id (v2)
            if "model" in model_data and isinstance(model_data["model"], dict):
                model = model_data["model"]
                return model.get("modelID") or model.get("id") or "unknown"
        return "unknown"

    @staticmethod
    def _extract_provider_id(message_json: Dict[str, Any]) -> Optional[str]:
        """Extract provider ID from top-level providerID (v1) or model.providerID (v2)."""
        provider_id = message_json.get("providerID")
        model = message_json.get("model")
        if not provider_id and isinstance(model, dict):
            provider_id = model.get("providerID")
        return provider_id.lower() if provider_id else None

    @staticmethod
    def _extract_tokens(message_json: Dict[str, Any]) -> TokenUsage:
        """Extract token usage from message JSON data."""
        tokens_data = message_json.get("tokens", {})
        cache_data = tokens_data.get("cache", {})

        return TokenUsage(
            input=max(0, tokens_data.get('input', 0)),
            output=max(0, tokens_data.get('output', 0)),
            cache_write=max(0, cache_data.get('write', 0)),
            cache_read=max(0, cache_data.get('read', 0))
        )

    @staticmethod
    def _extract_time_data(message_json: Dict[str, Any]) -> Optional[TimeData]:
        """Extract timing information from message JSON data."""
        time_data = message_json.get("time", {})
        if time_data:
            return TimeData(
                created=time_data.get("created"), completed=time_data.get("completed")
            )
        return None

    @staticmethod
    def _extract_project_path(message_json: Dict[str, Any]) -> Optional[str]:
        """Extract project path from message JSON data."""
        path_data = message_json.get("path", {})
        if path_data:
            # Prefer cwd, fallback to root
            return path_data.get("cwd") or path_data.get("root")
        return None

    @staticmethod
    def _extract_agent(message_json: Dict[str, Any]) -> Optional[str]:
        """Extract agent type from message JSON data."""
        return message_json.get("agent")

    @classmethod
    def parse_message_data(
        cls, message_data_str: str, session_id: str, role: Optional[str] = None
    ) -> Optional[InteractionFile]:
        """Parse a message data JSON string into an InteractionFile.

        Args:
            message_data_str: JSON string from message.data column
            session_id: ID of the session this message belongs to
            role: Message role when stored outside the JSON (v2 ``type`` column)

        Returns:
            InteractionFile object or None if parsing failed
        """
        try:
            data = json.loads(message_data_str)
        except (json.JSONDecodeError, TypeError):
            return None

        if not isinstance(data, dict):
            return None

        # Only process assistant messages with tokens
        role = role or data.get("role", "")
        if role != "assistant":
            return None

        tokens = cls._extract_tokens(data)
        time_data = cls._extract_time_data(data)
        project_path = cls._extract_project_path(data)
        agent = cls._extract_agent(data)
        model_id = cls._extract_model_name(data).lower()
        provider_id = cls._extract_provider_id(data)
        finish_reason = data.get("finish")

        return InteractionFile(
            file_path=Path("sqlite") / session_id,  # Placeholder path
            session_id=session_id,
            model_id=model_id,
            provider_id=provider_id,
            tokens=tokens,
            time_data=time_data,
            project_path=project_path,
            agent=agent,
            finish_reason=finish_reason,
            raw_data=data,
        )

    @classmethod
    def load_session_messages(
        cls, conn: sqlite3.Connection, session_id: str
    ) -> List[InteractionFile]:
        """Load all messages for a session.

        Args:
            conn: Database connection
            session_id: Session ID to load messages for

        Returns:
            List of InteractionFile objects
        """
        if cls._get_schema(conn) is _SCHEMA_V2:
            cursor = conn.execute(
                "SELECT data, type AS role FROM session_message "
                "WHERE session_id = ? AND type = 'assistant' ORDER BY seq",
                (session_id,),
            )
        else:
            cursor = conn.execute(
                "SELECT data, NULL AS role FROM message "
                "WHERE session_id = ? ORDER BY time_created",
                (session_id,),
            )

        interactions = []
        for row in cursor:
            interaction = cls.parse_message_data(row["data"], session_id, row["role"])
            if interaction:
                interactions.append(interaction)

        return interactions

    @classmethod
    def load_session_data(
        cls,
        conn: sqlite3.Connection,
        session_row: sqlite3.Row,
        include_zero_token: bool = False,
    ) -> Optional[SessionData]:
        """Load complete session data from a database row.

        Args:
            conn: Database connection
            session_row: Row from session table
            include_zero_token: Keep interactions with no token usage (providers
                that don't report usage), so the live view can still show them

        Returns:
            SessionData object or None if loading failed
        """
        session_id = session_row["id"]

        # Load all messages/interactions for this session
        interaction_files = cls.load_session_messages(conn, session_id)

        # Filter out zero-token interactions (consistent with FileProcessor)
        if not include_zero_token:
            interaction_files = [f for f in interaction_files if f.tokens.total > 0]

        if not interaction_files:
            return None

        # v2 messages no longer carry path.cwd; fall back to the session directory
        if "directory" in session_row.keys() and session_row["directory"]:
            for f in interaction_files:
                if not f.project_path:
                    f.project_path = session_row["directory"]

        # Get agent from first interaction
        sorted_files = sorted(
            interaction_files,
            key=lambda f: (
                f.time_data.created if f.time_data and f.time_data.created else 0
            ),
        )
        session_agent = sorted_files[0].agent if sorted_files else None

        # Determine if this is a sub-agent
        parent_id = session_row["parent_id"]
        is_sub_agent = parent_id is not None

        return SessionData(
            session_id=session_id,
            session_path=None,  # SQLite sessions don't have file paths
            parent_id=parent_id,
            is_sub_agent=is_sub_agent,
            files=interaction_files,
            session_title=session_row["title"],
            agent=session_agent,
            source="sqlite",
        )

    @classmethod
    def load_all_sessions(
        cls, db_path: Optional[Path] = None, limit: Optional[int] = None
    ) -> List[SessionData]:
        """Load all sessions from the SQLite database.

        Args:
            db_path: Path to database (uses default if not provided)
            limit: Maximum number of sessions to load (None for all)

        Returns:
            List of SessionData objects sorted by creation time (newest first)
        """
        if db_path is None:
            db_path = cls.find_database_path()

        if not db_path or not db_path.exists():
            return []

        conn = cls._get_connection(db_path)
        try:
            schema = cls._get_schema(conn)
            # Query all sessions with project info
            query = f"""
                SELECT s.*, p.worktree as project_path, p.name as project_name
                FROM {schema['session']} s
                LEFT JOIN project p ON s.project_id = p.id
                ORDER BY s.time_created DESC
            """

            cursor = conn.execute(query)
            sessions = []

            # Apply limit after filtering so sessions without token data
            # (e.g. providers that report no usage) don't consume the limit
            for row in cursor:
                session_data = cls.load_session_data(conn, row)
                if session_data:
                    sessions.append(session_data)
                    if limit and len(sessions) >= limit:
                        break

            return sessions
        finally:
            conn.close()

    @classmethod
    def load_session_hierarchy(cls, db_path: Optional[Path] = None) -> Dict[str, Any]:
        """Load sessions organized by parent-child hierarchy.

        Args:
            db_path: Path to database (uses default if not provided)

        Returns:
            Dictionary with:
                - 'root_sessions': List of parent sessions with sub_agents
                - 'all_sessions': Flat list of all sessions
                - 'source': 'sqlite'
        """
        all_sessions = cls.load_all_sessions(db_path)

        # Organize by parent_id
        parent_map: Dict[str, List[SessionData]] = {}
        root_sessions: List[Dict[str, Any]] = []
        session_lookup: Dict[str, SessionData] = {}

        for session in all_sessions:
            session_lookup[session.session_id] = session

        # Group sub-agents by parent
        for session in all_sessions:
            if session.parent_id:
                if session.parent_id not in parent_map:
                    parent_map[session.parent_id] = []
                parent_map[session.parent_id].append(session)

        # Build hierarchy
        for session in all_sessions:
            if not session.parent_id:  # Root session
                sub_agents = parent_map.get(session.session_id, [])
                # Sort sub-agents by creation time
                sub_agents.sort(key=lambda s: s.start_time or datetime.min)

                root_sessions.append({"session": session, "sub_agents": sub_agents})

        # Sort root sessions by creation time (newest first)
        root_sessions.sort(
            key=lambda x: x["session"].start_time or datetime.min, reverse=True
        )

        return {
            "root_sessions": root_sessions,
            "all_sessions": all_sessions,
            "source": "sqlite",
        }

    @classmethod
    def session_generator(
        cls, db_path: Optional[Path] = None
    ) -> Generator[SessionData, None, None]:
        """Generator that yields sessions one by one (memory efficient).

        Args:
            db_path: Path to database (uses default if not provided)

        Yields:
            SessionData objects
        """
        if db_path is None:
            db_path = cls.find_database_path()

        if not db_path or not db_path.exists():
            return

        conn = cls._get_connection(db_path)
        try:
            schema = cls._get_schema(conn)
            cursor = conn.execute(f"""
                SELECT s.*, p.worktree as project_path, p.name as project_name
                FROM {schema['session']} s
                LEFT JOIN project p ON s.project_id = p.id
                ORDER BY s.time_created DESC
            """)

            for row in cursor:
                session_data = cls.load_session_data(conn, row)
                if session_data:
                    yield session_data
        finally:
            conn.close()

    @classmethod
    def get_most_recent_workflow(
        cls, db_path: Optional[Path] = None
    ) -> Optional[Dict[str, Any]]:
        """Get the most recent workflow (parent session + sub-agents).

        Finds the most recent parent session that has actual message data
        (interactions), skipping empty sessions like ACP sessions.

        Args:
            db_path: Path to database (uses default if not provided)

        Returns:
            Dictionary with workflow data compatible with SessionWorkflow:
                - 'main_session': SessionData (parent session)
                - 'sub_agents': List[SessionData] (sorted by creation time)
                - 'all_sessions': List[SessionData] (main + sub-agents)
                - 'project_name': str
                - 'display_title': str
                - 'session_count': int (total sessions in workflow)
                - 'sub_agent_count': int (number of sub-agents)
                - 'has_sub_agents': bool
                - 'workflow_id': str (main session ID)

        Returns None if no sessions with data found.
        """
        if db_path is None:
            db_path = cls.find_database_path()

        if not db_path or not db_path.exists():
            return None

        conn = cls._get_connection(db_path)
        try:
            # Get recent parent sessions (no parent_id) and check which have messages
            # We check up to 10 recent parents to find one with actual data
            schema = cls._get_schema(conn)
            parent_rows = conn.execute(f"""
                SELECT s.*, p.worktree as project_path, p.name as project_name
                FROM {schema['session']} s
                LEFT JOIN project p ON s.project_id = p.id
                WHERE s.parent_id IS NULL
                ORDER BY s.time_created DESC
                LIMIT 10
            """).fetchall()

            if not parent_rows:
                return None

            # Try each parent until we find one with data
            main_session = None
            for parent_row in parent_rows:
                session = cls.load_session_data(
                    conn, parent_row, include_zero_token=True
                )
                if session and session.files:  # Has interactions
                    main_session = session
                    break

            if not main_session:
                return None

            return cls._build_workflow_dict(conn, main_session)
        finally:
            conn.close()

    @classmethod
    def _build_workflow_dict(
        cls, conn: sqlite3.Connection, main_session: SessionData
    ) -> Dict[str, Any]:
        """Build workflow payload with parent session and loaded sub-agents."""
        schema = cls._get_schema(conn)
        sub_agent_rows = conn.execute(
            f"""
            SELECT s.*, p.worktree as project_path, p.name as project_name
            FROM {schema['session']} s
            LEFT JOIN project p ON s.project_id = p.id
            WHERE s.parent_id = ?
            ORDER BY s.time_created ASC
        """,
            (main_session.session_id,),
        ).fetchall()

        sub_agents = []
        for row in sub_agent_rows:
            sub_session = cls.load_session_data(conn, row, include_zero_token=True)
            if sub_session:
                sub_agents.append(sub_session)

        all_sessions = [main_session] + sub_agents

        return {
            "main_session": main_session,
            "sub_agents": sub_agents,
            "all_sessions": all_sessions,
            "project_name": main_session.project_name,
            "display_title": main_session.display_title,
            "session_count": len(all_sessions),
            "sub_agent_count": len(sub_agents),
            "has_sub_agents": len(sub_agents) > 0,
            "workflow_id": main_session.session_id,
        }

    @classmethod
    def _find_orphan_subagent_workflows(
        cls,
        conn: sqlite3.Connection,
        threshold_ms: int,
        loaded_parent_ids: set,
    ) -> List[Dict[str, Any]]:
        """Find sub-agent workflows whose parents don't have token data.

        Some parent sessions (e.g., ACP sessions) may have messages but no token
        usage, causing them to be filtered out by load_session_data(). This method
        finds sub-agents that should be grouped together under a synthetic workflow.

        Args:
            conn: Database connection
            threshold_ms: Activity threshold in milliseconds
            loaded_parent_ids: Set of parent IDs that were already loaded successfully

        Returns:
            List of workflow dictionaries for orphan sub-agent groups
        """
        schema = cls._get_schema(conn)
        if loaded_parent_ids:
            placeholders = ",".join("?" * len(loaded_parent_ids))
            query = f"""
                SELECT s.*, p.worktree as project_path, p.name as project_name
                FROM {schema['session']} s
                LEFT JOIN project p ON s.project_id = p.id
                WHERE s.parent_id IS NOT NULL
                AND s.parent_id NOT IN ({placeholders})
                AND EXISTS (
                    SELECT 1 FROM {schema['message']} m
                    WHERE m.session_id = s.id AND m.time_created > ?
                )
                ORDER BY s.time_created DESC
            """
            params = list(loaded_parent_ids) + [threshold_ms]
        else:
            query = f"""
                SELECT s.*, p.worktree as project_path, p.name as project_name
                FROM {schema['session']} s
                LEFT JOIN project p ON s.project_id = p.id
                WHERE s.parent_id IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM {schema['message']} m
                    WHERE m.session_id = s.id AND m.time_created > ?
                )
                ORDER BY s.time_created DESC
            """
            params = [threshold_ms]

        sub_agent_rows = conn.execute(query, params).fetchall()

        sub_agents_by_parent: Dict[str, List[SessionData]] = {}
        for row in sub_agent_rows:
            sub_session = cls.load_session_data(conn, row, include_zero_token=True)
            if sub_session and sub_session.parent_id:
                parent_id: str = sub_session.parent_id
                if parent_id not in sub_agents_by_parent:
                    sub_agents_by_parent[parent_id] = []
                sub_agents_by_parent[parent_id].append(sub_session)

        orphan_workflows = []
        for parent_id, sub_agents in sub_agents_by_parent.items():
            if len(sub_agents) == 0:
                continue

            sub_agents.sort(key=lambda s: s.start_time or datetime.min)
            first_sub = sub_agents[0]

            remaining_subs = sub_agents[1:] if len(sub_agents) > 1 else []

            workflow = {
                "main_session": first_sub,
                "sub_agents": remaining_subs,
                "all_sessions": sub_agents,
                "project_name": first_sub.project_name,
                "display_title": first_sub.display_title
                or f"Orphan workflow ({parent_id[:12]}...)",
                "session_count": len(sub_agents),
                "sub_agent_count": len(remaining_subs),
                "has_sub_agents": len(remaining_subs) > 0,
                "workflow_id": parent_id,
                "is_orphan": True,
            }
            orphan_workflows.append(workflow)

        return orphan_workflows

    @classmethod
    def get_all_active_workflows(
        cls, db_path: Optional[Path] = None, active_threshold_minutes: int = 30
    ) -> List[Dict[str, Any]]:
        """Get all active workflows (parent sessions that are still ongoing).

        A workflow is considered active if its most recent message is within
        active_threshold_minutes (default 30). This avoids relying on time_archived
        which may not be set reliably when sessions end.

        Also handles orphan sub-agents whose parent sessions don't have token data
        (e.g., ACP sessions). These are grouped by parent_id and shown as workflows.

        Args:
            db_path: Path to database (uses default if not provided)
            active_threshold_minutes: Consider session active if activity within this window

        Returns:
            List of workflow dictionaries, sorted by most recent activity first.
            Each dict has the same structure as get_most_recent_workflow().
        """
        if db_path is None:
            db_path = cls.find_database_path()

        if not db_path or not db_path.exists():
            return []

        conn = cls._get_connection(db_path)
        try:
            threshold_ms = int(time.time() * 1000) - (
                active_threshold_minutes * 60 * 1000
            )

            schema = cls._get_schema(conn)
            parent_rows = conn.execute(
                f"""
                SELECT s.*, p.worktree as project_path, p.name as project_name,
                       MAX(m.time_created) as last_parent_message_time
                FROM {schema['session']} s
                LEFT JOIN project p ON s.project_id = p.id
                JOIN {schema['message']} m ON m.session_id = s.id
                WHERE s.parent_id IS NULL
                GROUP BY s.id
                HAVING last_parent_message_time > ?
                ORDER BY last_parent_message_time DESC
                LIMIT 30
            """,
                (threshold_ms,),
            ).fetchall()

            active_workflows = []
            loaded_parent_ids = set()

            for parent_row in parent_rows:
                session = cls.load_session_data(
                    conn, parent_row, include_zero_token=True
                )
                if session and session.files:
                    loaded_parent_ids.add(session.session_id)
                    active_workflows.append(cls._build_workflow_dict(conn, session))

            orphan_workflows = cls._find_orphan_subagent_workflows(
                conn, threshold_ms, loaded_parent_ids
            )
            active_workflows.extend(orphan_workflows)

            return active_workflows[:10]
        finally:
            conn.close()

    @classmethod
    def get_database_stats(cls, db_path: Optional[Path] = None) -> Dict[str, Any]:
        """Get statistics about the SQLite database.

        Args:
            db_path: Path to database (uses default if not provided)

        Returns:
            Dictionary with database statistics
        """
        if db_path is None:
            db_path = cls.find_database_path()

        if not db_path or not db_path.exists():
            return {"exists": False}

        conn = cls._get_connection(db_path)
        try:
            stats = {"exists": True, "path": str(db_path)}
            schema = cls._get_schema(conn)

            # Get counts
            stats["session_count"] = conn.execute(
                f"SELECT COUNT(*) FROM {schema['session']}"
            ).fetchone()[0]

            stats["message_count"] = conn.execute(
                f"SELECT COUNT(*) FROM {schema['message']}"
            ).fetchone()[0]

            stats["project_count"] = conn.execute(
                "SELECT COUNT(*) FROM project"
            ).fetchone()[0]

            # Get sub-agent count
            stats["sub_agent_count"] = conn.execute(
                f"SELECT COUNT(*) FROM {schema['session']} WHERE parent_id IS NOT NULL"
            ).fetchone()[0]

            # Get file size
            stats["file_size_bytes"] = db_path.stat().st_size

            return stats
        finally:
            conn.close()
    
    @classmethod
    def load_tool_usage_for_sessions(
        cls,
        session_ids: List[str],
        db_path: Optional[Path] = None
    ) -> List[ToolUsageStats]:
        """Load tool usage statistics for the given sessions.
        
        Queries the `part` table for tool entries with terminal statuses
        (completed or error) and aggregates counts by tool name.
        
        Args:
            session_ids: List of session IDs to aggregate tool usage for
            db_path: Path to database (uses default if not provided)
            
        Returns:
            List of ToolUsageStats sorted by total_calls descending
        """
        if not session_ids:
            return []
        
        if db_path is None:
            db_path = cls.find_database_path()
        
        if not db_path or not db_path.exists():
            return []
        
        conn = cls._get_connection(db_path)
        try:
            # Build placeholders for IN clause
            placeholders = ','.join('?' * len(session_ids))
            schema = cls._get_schema(conn)
            data = schema['tool_data']
            name_path = schema['tool_name_path']

            # Query tool parts with terminal statuses
            # Use json_valid to protect against malformed JSON
            # Use json_extract to access nested fields in the data column
            query = f"""
                SELECT
                    json_extract({data}, '{name_path}') as tool_name,
                    json_extract({data}, '$.state.status') as status,
                    COUNT(*) as count
                FROM {schema['tool_from']}
                WHERE {schema['tool_session']} IN ({placeholders})
                  AND json_valid({data}) = 1
                  AND json_extract({data}, '$.type') = 'tool'
                  AND json_extract({data}, '{name_path}') IS NOT NULL
                  AND json_extract({data}, '$.state.status') IN ('completed', 'error')
                GROUP BY tool_name, status
            """
            
            cursor = conn.execute(query, session_ids)
            
            # Aggregate by tool name
            tool_data: Dict[str, Dict[str, int]] = {}
            for row in cursor:
                tool_name = row['tool_name']
                status = row['status']
                count = row['count']
                
                if tool_name not in tool_data:
                    tool_data[tool_name] = {'success': 0, 'failure': 0}
                
                if status == 'completed':
                    tool_data[tool_name]['success'] += count
                elif status == 'error':
                    tool_data[tool_name]['failure'] += count
            
            # Build ToolUsageStats list
            stats = []
            for tool_name, counts in tool_data.items():
                total = counts['success'] + counts['failure']
                stats.append(ToolUsageStats(
                    tool_name=tool_name,
                    total_calls=total,
                    success_count=counts['success'],
                    failure_count=counts['failure']
                ))
            
            # Sort by total_calls descending
            stats.sort(key=lambda s: s.total_calls, reverse=True)
            
            return stats
        finally:
            conn.close()

    @classmethod
    def load_tool_usage_by_model_for_sessions(
        cls,
        session_ids: List[str],
        db_path: Optional[Path] = None
    ) -> List[ModelToolUsage]:
        """Load tool usage statistics grouped by model for the given sessions.
        
        Queries the `part` table joined with `message` to get model information
        for each tool call. Aggregates counts by model and tool name.
        
        Args:
            session_ids: List of session IDs to aggregate tool usage for
            db_path: Path to database (uses default if not provided)
            
        Returns:
            List of ModelToolUsage sorted by total_calls descending
        """
        if not session_ids:
            return []
        
        if db_path is None:
            db_path = cls.find_database_path()
        
        if not db_path or not db_path.exists():
            return []
        
        conn = cls._get_connection(db_path)
        try:
            placeholders = ','.join('?' * len(session_ids))
            schema = cls._get_schema(conn)
            data = schema['tool_data']
            name_path = schema['tool_name_path']

            query = f"""
                SELECT
                    COALESCE({schema['model']}, 'unknown') as model_id,
                    json_extract({data}, '{name_path}') as tool_name,
                    json_extract({data}, '$.state.status') as status,
                    COUNT(*) as count
                FROM {schema['tool_from_with_message']}
                WHERE {schema['tool_session']} IN ({placeholders})
                  AND json_valid({data}) = 1
                  AND json_valid(m.data) = 1
                  AND json_extract({data}, '$.type') = 'tool'
                  AND json_extract({data}, '{name_path}') IS NOT NULL
                  AND json_extract({data}, '$.state.status') IN ('completed', 'error')
                GROUP BY model_id, tool_name, status
            """
            
            cursor = conn.execute(query, session_ids)
            
            model_data: Dict[str, Dict[str, Dict[str, int]]] = {}
            for row in cursor:
                model_id = row['model_id']
                tool_name = row['tool_name']
                status = row['status']
                count = row['count']
                
                if model_id not in model_data:
                    model_data[model_id] = {}
                if tool_name not in model_data[model_id]:
                    model_data[model_id][tool_name] = {'success': 0, 'failure': 0}
                
                if status == 'completed':
                    model_data[model_id][tool_name]['success'] += count
                elif status == 'error':
                    model_data[model_id][tool_name]['failure'] += count
            
            result = []
            for model_name, tools in model_data.items():
                tool_stats = []
                for tool_name, counts in tools.items():
                    total = counts['success'] + counts['failure']
                    tool_stats.append(ToolUsageStats(
                        tool_name=tool_name,
                        total_calls=total,
                        success_count=counts['success'],
                        failure_count=counts['failure']
                    ))
                tool_stats.sort(key=lambda s: s.total_calls, reverse=True)
                result.append(ModelToolUsage(
                    model_name=model_name,
                    tool_stats=tool_stats
                ))
            
            result.sort(key=lambda m: m.total_calls, reverse=True)

            return result
        finally:
            conn.close()

    @classmethod
    def find_matching_models(
        cls, query: str, db_path: Optional[Path] = None
    ) -> List[str]:
        """Find model names matching a query string (case-insensitive substring match).

        Args:
            query: Substring to search for in model names
            db_path: Path to database (uses default if not provided)

        Returns:
            List of distinct model name strings matching the query
        """
        if db_path is None:
            db_path = cls.find_database_path()

        if not db_path or not db_path.exists():
            return []

        conn = cls._get_connection(db_path)
        try:
            schema = cls._get_schema(conn)
            cursor = conn.execute(
                f"""
                SELECT DISTINCT
                    LOWER(COALESCE({schema['model']}, 'unknown')) as model_name
                FROM {schema['message']} m
                WHERE json_valid(m.data) = 1
                  AND {schema['is_assistant']}
                  AND LOWER(COALESCE({schema['model']}, 'unknown')) LIKE ?
                ORDER BY model_name
                """,
                (f"%{query.lower()}%",),
            )
            return [row["model_name"] for row in cursor if row["model_name"] != "unknown"]
        finally:
            conn.close()

    @classmethod
    def get_model_detail_stats(
        cls, model_name: str, pricing_data: Dict[str, Any], db_path: Optional[Path] = None
    ) -> Optional[ModelDetailStats]:
        """Get detailed statistics for a single model.

        Args:
            model_name: Exact model name to query
            pricing_data: Model pricing information for cost calculation
            db_path: Path to database (uses default if not provided)

        Returns:
            ModelDetailStats object or None if no data found
        """
        if db_path is None:
            db_path = cls.find_database_path()

        if not db_path or not db_path.exists():
            return None

        conn = cls._get_connection(db_path)
        try:
            schema = cls._get_schema(conn)
            # Query 1: Basic aggregate stats
            row = conn.execute(
                f"""
                SELECT
                    MIN(json_extract(m.data, '$.time.created')) as first_used,
                    MAX(COALESCE(
                        json_extract(m.data, '$.time.completed'),
                        json_extract(m.data, '$.time.created')
                    )) as last_used,
                    COUNT(DISTINCT m.session_id) as total_sessions,
                    COUNT(DISTINCT date(
                        json_extract(m.data, '$.time.created') / 1000, 'unixepoch'
                    )) as total_days,
                    COUNT(m.id) as total_interactions,
                    SUM(MAX(0, COALESCE(json_extract(m.data, '$.tokens.input'), 0))) as input_tokens,
                    SUM(MAX(0, COALESCE(json_extract(m.data, '$.tokens.output'), 0))) as output_tokens,
                    SUM(MAX(0, COALESCE(json_extract(m.data, '$.tokens.cache.read'), 0))) as cache_read,
                    SUM(MAX(0, COALESCE(json_extract(m.data, '$.tokens.cache.write'), 0))) as cache_write
                FROM {schema['message']} m
                WHERE json_valid(m.data) = 1
                  AND {schema['is_assistant']}
                  AND LOWER({schema['model']}) = ?
                """,
                (model_name.lower(),),
            ).fetchone()

            if not row or row["total_interactions"] == 0:
                return None

            # Parse timestamps
            first_used = None
            last_used = None
            if row["first_used"]:
                try:
                    first_used = datetime.fromtimestamp(row["first_used"] / 1000)
                except (OSError, ValueError):
                    pass
            if row["last_used"]:
                try:
                    last_used = datetime.fromtimestamp(row["last_used"] / 1000)
                except (OSError, ValueError):
                    pass

            tokens = TokenUsage(
                input=max(0, row["input_tokens"] or 0),
                output=max(0, row["output_tokens"] or 0),
                cache_read=max(0, row["cache_read"] or 0),
                cache_write=max(0, row["cache_write"] or 0),
            )

            # Calculate cost using pricing data
            total_cost = Decimal('0.0')
            model_pricing = FileProcessor.lookup_pricing(
                pricing_data,
                model_id=model_name,
                provider_id=None,
            )
            if model_pricing:
                price_input = getattr(model_pricing, 'input', 0) or 0
                price_output = getattr(model_pricing, 'output', 0) or 0
                price_cache_read = getattr(model_pricing, 'cacheRead', 0) or 0
                price_cache_write = getattr(model_pricing, 'cacheWrite', 0) or 0
                total_cost = (
                    Decimal(str(tokens.input)) * Decimal(str(price_input)) / Decimal('1000000')
                    + Decimal(str(tokens.output)) * Decimal(str(price_output)) / Decimal('1000000')
                    + Decimal(str(tokens.cache_read)) * Decimal(str(price_cache_read)) / Decimal('1000000')
                    + Decimal(str(tokens.cache_write)) * Decimal(str(price_cache_write)) / Decimal('1000000')
                )

            total_sessions = row["total_sessions"]
            total_days = row["total_days"]
            avg_cost_per_day = total_cost / total_days if total_days > 0 else Decimal('0.0')
            avg_cost_per_session = total_cost / total_sessions if total_sessions > 0 else Decimal('0.0')

            # Query 2: Per-interaction output rates for p50 calculation
            rate_cursor = conn.execute(
                f"""
                SELECT
                    COALESCE(json_extract(m.data, '$.tokens.output'), 0) as output_tokens,
                    json_extract(m.data, '$.time.created') as created,
                    json_extract(m.data, '$.time.completed') as completed,
                    json_extract(m.data, '$.finish') as finish_reason
                FROM {schema['message']} m
                WHERE json_valid(m.data) = 1
                  AND {schema['is_assistant']}
                  AND LOWER({schema['model']}) = ?
                """,
                (model_name.lower(),),
            )

            interaction_rates = []
            for rate_row in rate_cursor:
                output_tok = rate_row["output_tokens"] or 0
                created_ms = rate_row["created"]
                completed_ms = rate_row["completed"]
                finish = rate_row["finish_reason"]

                if not created_ms or not completed_ms:
                    continue
                duration_ms = completed_ms - created_ms
                if duration_ms <= 0 or output_tok < 100:
                    continue
                # Skip tool-call-only interactions
                if finish == "tool-calls":
                    continue
                rate = output_tok / (duration_ms / 1000)
                interaction_rates.append(rate)

            # Query 3: Tool usage stats
            data = schema['tool_data']
            name_path = schema['tool_name_path']
            tool_cursor = conn.execute(
                f"""
                SELECT
                    json_extract({data}, '{name_path}') as tool_name,
                    COUNT(*) as total_calls,
                    SUM(CASE WHEN json_extract({data}, '$.state.status') = 'completed'
                        THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN json_extract({data}, '$.state.status') = 'error'
                        THEN 1 ELSE 0 END) as failed
                FROM {schema['tool_from_with_message']}
                WHERE json_valid({data}) = 1
                  AND json_valid(m.data) = 1
                  AND json_extract({data}, '$.type') = 'tool'
                  AND json_extract({data}, '{name_path}') IS NOT NULL
                  AND json_extract({data}, '$.state.status') IN ('completed', 'error')
                  AND LOWER({schema['model']}) = ?
                GROUP BY tool_name
                ORDER BY total_calls DESC
                """,
                (model_name.lower(),),
            )

            tool_stats = []
            for tool_row in tool_cursor:
                tool_stats.append(ToolUsageStats(
                    tool_name=tool_row["tool_name"],
                    total_calls=tool_row["total_calls"],
                    success_count=tool_row["success"],
                    failure_count=tool_row["failed"],
                ))

            tool_summary = ToolUsageSummary(tool_stats=tool_stats)

            return ModelDetailStats(
                model_name=model_name,
                first_used=first_used,
                last_used=last_used,
                total_sessions=total_sessions,
                total_days_used=total_days,
                total_interactions=row["total_interactions"],
                total_tokens=tokens,
                total_cost=total_cost,
                avg_cost_per_day=avg_cost_per_day,
                avg_cost_per_session=avg_cost_per_session,
                interaction_rates=interaction_rates,
                tool_stats=tool_stats,
                tool_summary=tool_summary,
            )
        finally:
            conn.close()
