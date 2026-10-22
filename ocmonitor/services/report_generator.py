"""Report generation service for OpenCode Monitor."""

from decimal import Decimal
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel

from ..models.analytics import (
    DailyUsage,
    ModelBreakdownReport,
    MonthlyUsage,
    ProjectBreakdownReport,
    WeeklyUsage,
)
from ..models.session import SessionData
from ..services.session_analyzer import SessionAnalyzer
from ..services.session_grouper import SessionGrouper
from ..ui.tables import TableFormatter
from ..utils.file_utils import FileProcessor


class ReportGenerator:
    """Service for generating various types of reports."""

    def __init__(
        self,
        analyzer: SessionAnalyzer,
        console: Optional[Console] = None,
        currency_converter=None,
    ):
        """Initialize report generator.

        Args:
            analyzer: SessionAnalyzer instance
            console: Rich console for output
            currency_converter: Optional CurrencyConverter for currency formatting
        """
        self.analyzer = analyzer
        self.currency_converter = currency_converter
        self.table_formatter = TableFormatter(console, currency_converter)
        self.console = console or Console()

    def _fmt_cost(self, amount: Decimal) -> str:
        """Format cost amount using currency converter."""
        if self.currency_converter:
            return self.currency_converter.format(amount)
        return f"${amount:.2f}"

    def _get_model_breakdown_for_sessions(
        self, sessions: List[SessionData], force_recalculate: bool = False
    ) -> List[Dict[str, Any]]:
        """Calculate per-model breakdown for a set of sessions.

        Args:
            sessions: List of sessions to analyze
            force_recalculate: If True, ignore stored costs and recalculate from pricing data

        Returns:
            List of model breakdown dicts sorted by cost descending
        """
        by_display_model: Dict[str, Dict[str, Any]] = {}

        for session in sessions:
            for file in session.files:
                display_model = file.display_model
                if display_model not in by_display_model:
                    by_display_model[display_model] = {
                        "sessions": set(),
                        "interactions": 0,
                        "tokens": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read": 0,
                        "cache_write": 0,
                        "cost": Decimal("0.0"),
                    }
                by_display_model[display_model]["sessions"].add(session.session_id)
                by_display_model[display_model]["interactions"] += 1
                by_display_model[display_model]["tokens"] += file.tokens.total
                by_display_model[display_model]["input_tokens"] += file.tokens.input
                by_display_model[display_model]["output_tokens"] += file.tokens.output
                by_display_model[display_model]["cache_read"] += file.tokens.cache_read
                by_display_model[display_model]["cache_write"] += file.tokens.cache_write
                by_display_model[display_model]["cost"] += file.calculate_cost(
                    self.analyzer.pricing_data, force_recalculate
                )

        results = []
        for display_model, data in by_display_model.items():
            results.append(
                {
                    "model": display_model,
                    "sessions": len(data["sessions"]),
                    "interactions": data["interactions"],
                    "tokens": data["tokens"],
                    "input_tokens": data["input_tokens"],
                    "output_tokens": data["output_tokens"],
                    "cache_read": data["cache_read"],
                    "cache_write": data["cache_write"],
                    "cost": data["cost"],
                }
            )

        return sorted(results, key=lambda x: x["cost"], reverse=True)

    def generate_single_session_report(
        self,
        session_path: str,
        output_format: str = "table",
        force_recalculate: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Generate report for a single session.

        Args:
            session_path: Path to session directory
            output_format: Output format ("table", "json", "csv")
            force_recalculate: If True, ignore stored costs and recalculate from pricing data

        Returns:
            Report data or None if session not found
        """
        session = self.analyzer.analyze_single_session(session_path)
        if not session:
            return None

        # Get detailed statistics
        stats = self.analyzer.get_session_statistics(session, force_recalculate)
        health = self.analyzer.validate_session_health(session, force_recalculate)

        report_data = {
            "type": "single_session",
            "session": session,
            "statistics": stats,
            "health": health,
            "recalculated": force_recalculate,
        }

        if output_format == "table":
            self._display_single_session_table(
                session, stats, health, force_recalculate
            )
        elif output_format == "json":
            return self._format_single_session_json(
                session, stats, health, force_recalculate
            )
        elif output_format == "csv":
            return self._format_single_session_csv(session, stats, force_recalculate)

        return report_data

    def generate_sessions_summary_report(
        self,
        base_path: str,
        limit: Optional[int] = None,
        output_format: str = "table",
        group_workflows: bool = True,
        force_recalculate: bool = False,
    ) -> Dict[str, Any]:
        """Generate summary report for all sessions.

        Args:
            base_path: Path to directory containing sessions
            limit: Maximum number of sessions to analyze
            output_format: Output format ("table", "json", "csv")
            group_workflows: Group sessions by workflow (main + sub-agents)
            force_recalculate: If True, ignore stored costs and recalculate from pricing data

        Returns:
            Report data
        """
        sessions = self.analyzer.analyze_all_sessions(base_path, limit)
        summary = self.analyzer.get_sessions_summary(sessions, force_recalculate)

        report_data = {
            "type": "sessions_summary",
            "sessions": sessions,
            "summary": summary,
            "recalculated": force_recalculate,
        }

        if output_format == "table":
            if group_workflows:
                self._display_workflow_sessions_table(
                    sessions, summary, force_recalculate
                )
            else:
                self._display_sessions_summary_table(
                    sessions, summary, force_recalculate
                )
        elif output_format == "json":
            return self._format_sessions_summary_json(
                sessions, summary, force_recalculate
            )
        elif output_format == "csv":
            return self._format_sessions_summary_csv(sessions, force_recalculate)

        return report_data

    def generate_daily_report(
        self,
        base_path: str,
        month: Optional[str] = None,
        output_format: str = "table",
        breakdown: bool = False,
        last_n_days: Optional[int] = None,
        year: Optional[int] = None,
        force_recalculate: bool = False,
    ) -> Dict[str, Any]:
        """Generate daily breakdown report.

        Args:
            base_path: Path to directory containing sessions
            month: Optional month filter (YYYY-MM format)
            output_format: Output format ("table", "json", "csv")
            last_n_days: Optional - if set, filter to last N days
            year: Optional year filter (YYYY)
            force_recalculate: If True, ignore stored costs and recalculate from pricing data

        Returns:
            Report data
        """
        sessions = self.analyzer.analyze_all_sessions(base_path)

        from ..utils.time_utils import TimeUtils

        if month:
            month_data = TimeUtils.parse_month_string(month)
            if month_data:
                year_num, month_num = month_data
                start_date, end_date = TimeUtils.get_month_range(year_num, month_num)
                sessions = self.analyzer.filter_sessions_by_date(
                    sessions, start_date, end_date
                )
        elif last_n_days:
            start_date, end_date = TimeUtils.get_last_n_days_range(last_n_days)
            sessions = self.analyzer.filter_sessions_by_date(
                sessions, start_date, end_date
            )
        elif year:
            start_date, end_date = TimeUtils.get_year_range(year)
            sessions = self.analyzer.filter_sessions_by_date(
                sessions, start_date, end_date
            )

        daily_usage = self.analyzer.create_daily_breakdown(sessions)

        if month:
            filter_desc = {"month": month}
            filter_label = "month: " + month
        elif last_n_days:
            filter_desc = {"last_n_days": last_n_days}
            filter_label = "last " + str(last_n_days) + " days"
        elif year:
            filter_desc = {"year": year}
            filter_label = "year: " + str(year)
        else:
            filter_desc = None
            filter_label = None

        report_data = {
            "type": "daily_breakdown",
            "daily_usage": daily_usage,
            "filter": filter_desc,
            "filter_label": filter_label,
            "recalculated": force_recalculate,
        }

        if output_format == "table":
            self._display_daily_breakdown_table(
                daily_usage, breakdown, force_recalculate
            )
        elif output_format == "json":
            result = self._format_daily_breakdown_json(daily_usage, force_recalculate)
            result["type"] = "daily_breakdown"
            result["filter"] = filter_desc
            result["filter_label"] = filter_label
            result["recalculated"] = force_recalculate
            return result
        elif output_format == "csv":
            return self._format_daily_breakdown_csv(daily_usage, force_recalculate)

        return report_data

    def generate_weekly_report(
        self,
        base_path: str,
        year: Optional[int] = None,
        month: Optional[str] = None,
        output_format: str = "table",
        breakdown: bool = False,
        week_start_day: int = 0,
        last_n_days: Optional[int] = None,
        force_recalculate: bool = False,
    ) -> Dict[str, Any]:
        """Generate weekly breakdown report.

        Args:
            base_path: Path to directory containing sessions
            year: Optional year filter (YYYY)
            month: Optional month filter (YYYY-MM format)
            output_format: Output format ("table", "json", "csv")
            breakdown: Show per-model breakdown
            week_start_day: Day to start week on (0=Monday, 6=Sunday)
            last_n_days: Optional - if set, filter to last N days
            force_recalculate: If True, ignore stored costs and recalculate from pricing data

        Returns:
            Report data
        """
        sessions = self.analyzer.analyze_all_sessions(base_path)

        from ..utils.time_utils import TimeUtils

        if month:
            month_data = TimeUtils.parse_month_string(month)
            if month_data:
                year_num, month_num = month_data
                start_date, end_date = TimeUtils.get_month_range(year_num, month_num)
                sessions = self.analyzer.filter_sessions_by_date(
                    sessions, start_date, end_date
                )
        elif year:
            start_date, end_date = TimeUtils.get_year_range(year)
            sessions = self.analyzer.filter_sessions_by_date(
                sessions, start_date, end_date
            )
        elif last_n_days:
            start_date, end_date = TimeUtils.get_last_n_days_range(last_n_days)
            sessions = self.analyzer.filter_sessions_by_date(
                sessions, start_date, end_date
            )

        weekly_usage = self.analyzer.create_weekly_breakdown(sessions, week_start_day)

        if month:
            filter_desc = {"month": month, "week_start_day": week_start_day}
            filter_label = "month: " + month
        elif year:
            filter_desc = {"year": year, "week_start_day": week_start_day}
            filter_label = "year: " + str(year)
        elif last_n_days:
            filter_desc = {"last_n_days": last_n_days, "week_start_day": week_start_day}
            filter_label = "last " + str(last_n_days) + " days"
        else:
            if week_start_day != 0:
                filter_desc = {"week_start_day": week_start_day}
            else:
                filter_desc = None
            filter_label = None

        report_data = {
            "type": "weekly_breakdown",
            "weekly_usage": weekly_usage,
            "filter": filter_desc,
            "filter_label": filter_label,
            "recalculated": force_recalculate,
        }

        if output_format == "table":
            self._display_weekly_breakdown_table(
                weekly_usage, breakdown, week_start_day, force_recalculate
            )
        elif output_format == "json":
            result = self._format_weekly_breakdown_json(weekly_usage, force_recalculate)
            result["type"] = "weekly_breakdown"
            result["filter"] = filter_desc
            result["filter_label"] = filter_label
            result["recalculated"] = force_recalculate
            return result
        elif output_format == "csv":
            return self._format_weekly_breakdown_csv(weekly_usage, force_recalculate)

        return report_data

    def generate_monthly_report(
        self,
        base_path: str,
        year: Optional[int] = None,
        output_format: str = "table",
        breakdown: bool = False,
        last_n_days: Optional[int] = None,
        force_recalculate: bool = False,
    ) -> Dict[str, Any]:
        """Generate monthly breakdown report.

        Args:
            base_path: Path to directory containing sessions
            year: Optional year filter
            output_format: Output format ("table", "json", "csv")
            last_n_days: Optional - if set, filter to last N days
            force_recalculate: If True, ignore stored costs and recalculate from pricing data

        Returns:
            Report data
        """
        sessions = self.analyzer.analyze_all_sessions(base_path)

        from ..utils.time_utils import TimeUtils

        if year:
            start_date, end_date = TimeUtils.get_year_range(year)
            sessions = self.analyzer.filter_sessions_by_date(
                sessions, start_date, end_date
            )
        elif last_n_days:
            start_date, end_date = TimeUtils.get_last_n_days_range(last_n_days)
            sessions = self.analyzer.filter_sessions_by_date(
                sessions, start_date, end_date
            )

        monthly_usage = self.analyzer.create_monthly_breakdown(sessions)

        if year:
            filter_desc = {"year": year}
            filter_label = "year: " + str(year)
        elif last_n_days:
            filter_desc = {"last_n_days": last_n_days}
            filter_label = "last " + str(last_n_days) + " days"
        else:
            filter_desc = None
            filter_label = None

        report_data = {
            "type": "monthly_breakdown",
            "monthly_usage": monthly_usage,
            "filter": filter_desc,
            "filter_label": filter_label,
            "recalculated": force_recalculate,
        }

        if output_format == "table":
            self._display_monthly_breakdown_table(
                monthly_usage, breakdown, force_recalculate
            )
        elif output_format == "json":
            result = self._format_monthly_breakdown_json(
                monthly_usage, force_recalculate
            )
            result["type"] = "monthly_breakdown"
            result["filter"] = filter_desc
            result["filter_label"] = filter_label
            result["recalculated"] = force_recalculate
            return result
        elif output_format == "csv":
            return self._format_monthly_breakdown_csv(monthly_usage, force_recalculate)

        return report_data

    def generate_models_report(
        self,
        base_path: str,
        timeframe: str = "all",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        output_format: str = "table",
        force_recalculate: bool = False,
    ) -> Dict[str, Any]:
        """Generate model usage breakdown report.

        Args:
            base_path: Path to directory containing sessions
            timeframe: Timeframe for analysis
            start_date: Start date (YYYY-MM-DD format)
            end_date: End date (YYYY-MM-DD format)
            output_format: Output format ("table", "json", "csv")
            force_recalculate: If True, ignore stored costs and recalculate from pricing data

        Returns:
            Report data
        """
        sessions = self.analyzer.analyze_all_sessions(base_path)

        # Parse date filters
        from ..utils.time_utils import TimeUtils

        parsed_start_date = (
            TimeUtils.parse_date_string(start_date) if start_date else None
        )
        parsed_end_date = TimeUtils.parse_date_string(end_date) if end_date else None

        # Convert timeframe to date range if no explicit dates provided
        if timeframe != "all" and not parsed_start_date and not parsed_end_date:
            if timeframe == "weekly":
                parsed_start_date, parsed_end_date = TimeUtils.get_current_week_range()
            elif timeframe == "monthly":
                parsed_start_date, parsed_end_date = TimeUtils.get_current_month_range()
            elif timeframe == "daily":
                parsed_start_date, parsed_end_date = TimeUtils.get_last_n_days_range(1)
            sessions = self.analyzer.filter_sessions_by_date(
                sessions, parsed_start_date, parsed_end_date
            )

        model_breakdown = self.analyzer.create_model_breakdown(
            sessions, timeframe, parsed_start_date, parsed_end_date, force_recalculate
        )

        report_data = {
            "type": "models_breakdown",
            "model_breakdown": model_breakdown,
            "filter": {
                "timeframe": timeframe,
                "start_date": parsed_start_date.isoformat()
                if parsed_start_date
                else None,
                "end_date": parsed_end_date.isoformat() if parsed_end_date else None,
            },
            "recalculated": force_recalculate,
        }

        if output_format == "table":
            self._display_models_breakdown_table(model_breakdown)
        elif output_format == "json":
            return self._format_models_breakdown_json(
                model_breakdown, force_recalculate
            )
        elif output_format == "csv":
            return self._format_models_breakdown_csv(model_breakdown, force_recalculate)

        return report_data

    def generate_model_detail_report(
        self, name: str, output_format: str = "table"
    ) -> Optional[Dict[str, Any]]:
        """Generate a detailed report for a single model.

        Handles fuzzy matching: 0 matches shows available models,
        >1 matches shows list, 1 match shows full detail.

        Args:
            name: Model name query (fuzzy matched)
            output_format: Output format ("table", "json", "csv")

        Returns:
            Report data dict, or None if no match
        """
        matches = self.analyzer.find_matching_models(name)

        if len(matches) == 0:
            # No matches - show available models
            all_models = self.analyzer.find_matching_models("")
            self.console.print(
                f"[status.error]No model found matching '{name}'.[/status.error]"
            )
            if all_models:
                self.console.print("\n[metric.label]Available models:[/metric.label]")
                for m in all_models:
                    self.console.print(f"  - [table.row.model]{m}[/table.row.model]")
            return None

        if len(matches) > 1:
            # Check for exact match first
            exact = [m for m in matches if m == name.lower()]
            if len(exact) == 1:
                matches = exact
            else:
                self.console.print(
                    f"[status.warning]Multiple models match '{name}'. "
                    f"Did you mean one of these?[/status.warning]"
                )
                for i, m in enumerate(matches, 1):
                    self.console.print(f"  {i}. [table.row.model]{m}[/table.row.model]")
                return None

        model_name = matches[0]
        stats = self.analyzer.get_model_detail(model_name)

        if stats is None:
            self.console.print(
                f"[status.error]No usage data found for model '{model_name}'.[/status.error]"
            )
            return None

        report_data = {
            "type": "model_detail",
            "model_detail": stats,
        }

        if output_format == "table":
            self._display_model_detail(stats)
        elif output_format == "json":
            return self._format_model_detail_json(stats)
        elif output_format == "csv":
            return self._format_model_detail_csv(stats)

        return report_data

    def _display_model_detail(self, stats):
        """Display model detail panel and tool table."""
        from ..models.analytics import ModelDetailStats

        panel = self.table_formatter.create_model_detail_panel(stats)
        self.console.print(panel)

        if stats.tool_stats:
            tool_table = self.table_formatter.create_model_tool_table(
                stats.tool_stats, stats.model_name
            )
            self.console.print(tool_table)

            # Tool summary line
            summary = stats.tool_summary
            self.console.print(
                f"\n[metric.label]Total tool calls:[/metric.label] "
                f"[metric.value]{summary.total_calls:,}[/metric.value]  "
                f"[status.success]{summary.total_success:,} succeeded[/status.success]  "
                f"[status.error]{summary.total_failures:,} failed[/status.error]  "
                f"[metric.label]Overall:[/metric.label] "
                f"[metric.value]{summary.overall_success_rate:.0f}%[/metric.value]"
            )

    def _format_model_detail_json(self, stats) -> Dict[str, Any]:
        """Format model detail as JSON."""
        return {
            "model_name": stats.model_name,
            "first_used": stats.first_used.isoformat() if stats.first_used else None,
            "last_used": stats.last_used.isoformat() if stats.last_used else None,
            "total_sessions": stats.total_sessions,
            "total_days_used": stats.total_days_used,
            "total_interactions": stats.total_interactions,
            "tokens": stats.total_tokens.model_dump(),
            "total_cost": float(stats.total_cost),
            "avg_cost_per_day": float(stats.avg_cost_per_day),
            "avg_cost_per_session": float(stats.avg_cost_per_session),
            "p50_output_rate": stats.p50_output_rate,
            "tool_stats": [
                {
                    "tool_name": t.tool_name,
                    "total_calls": t.total_calls,
                    "success_count": t.success_count,
                    "failure_count": t.failure_count,
                    "success_rate": t.success_rate,
                }
                for t in stats.tool_stats
            ],
            "tool_summary": {
                "total_calls": stats.tool_summary.total_calls,
                "total_success": stats.tool_summary.total_success,
                "total_failures": stats.tool_summary.total_failures,
                "overall_success_rate": stats.tool_summary.overall_success_rate,
            },
        }

    def _format_model_detail_csv(self, stats) -> List[Dict[str, Any]]:
        """Format model detail as CSV rows (one row per tool)."""
        if not stats.tool_stats:
            return [
                {
                    "model_name": stats.model_name,
                    "total_sessions": stats.total_sessions,
                    "total_days_used": stats.total_days_used,
                    "total_interactions": stats.total_interactions,
                    "input_tokens": stats.total_tokens.input,
                    "output_tokens": stats.total_tokens.output,
                    "cache_read_tokens": stats.total_tokens.cache_read,
                    "cache_write_tokens": stats.total_tokens.cache_write,
                    "total_cost": float(stats.total_cost),
                    "avg_cost_per_day": float(stats.avg_cost_per_day),
                    "avg_cost_per_session": float(stats.avg_cost_per_session),
                    "p50_output_rate": stats.p50_output_rate,
                    "tool_name": "",
                    "tool_calls": 0,
                    "tool_success": 0,
                    "tool_failed": 0,
                    "tool_success_rate": 0.0,
                }
            ]

        rows = []
        for t in stats.tool_stats:
            rows.append(
                {
                    "model_name": stats.model_name,
                    "total_sessions": stats.total_sessions,
                    "total_days_used": stats.total_days_used,
                    "total_interactions": stats.total_interactions,
                    "input_tokens": stats.total_tokens.input,
                    "output_tokens": stats.total_tokens.output,
                    "cache_read_tokens": stats.total_tokens.cache_read,
                    "cache_write_tokens": stats.total_tokens.cache_write,
                    "total_cost": float(stats.total_cost),
                    "avg_cost_per_day": float(stats.avg_cost_per_day),
                    "avg_cost_per_session": float(stats.avg_cost_per_session),
                    "p50_output_rate": stats.p50_output_rate,
                    "tool_name": t.tool_name,
                    "tool_calls": t.total_calls,
                    "tool_success": t.success_count,
                    "tool_failed": t.failure_count,
                    "tool_success_rate": t.success_rate,
                }
            )
        return rows

    def generate_projects_report(
        self,
        base_path: str,
        timeframe: str = "all",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        output_format: str = "table",
        force_recalculate: bool = False,
    ) -> Dict[str, Any]:
        """Generate project usage breakdown report.

        Args:
            base_path: Path to directory containing sessions
            timeframe: Timeframe for analysis
            start_date: Start date (YYYY-MM-DD format)
            end_date: End date (YYYY-MM-DD format)
            output_format: Output format ("table", "json", "csv")
            force_recalculate: If True, ignore stored costs and recalculate from pricing data

        Returns:
            Report data
        """
        sessions = self.analyzer.analyze_all_sessions(base_path)

        # Parse date filters
        from ..utils.time_utils import TimeUtils

        parsed_start_date = (
            TimeUtils.parse_date_string(start_date) if start_date else None
        )
        parsed_end_date = TimeUtils.parse_date_string(end_date) if end_date else None

        # Convert timeframe to date range if no explicit dates provided
        if timeframe != "all" and not parsed_start_date and not parsed_end_date:
            if timeframe == "weekly":
                parsed_start_date, parsed_end_date = TimeUtils.get_current_week_range()
            elif timeframe == "monthly":
                parsed_start_date, parsed_end_date = TimeUtils.get_current_month_range()
            elif timeframe == "daily":
                parsed_start_date, parsed_end_date = TimeUtils.get_last_n_days_range(1)
            sessions = self.analyzer.filter_sessions_by_date(
                sessions, parsed_start_date, parsed_end_date
            )

        project_breakdown = self.analyzer.create_project_breakdown(
            sessions, timeframe, parsed_start_date, parsed_end_date, force_recalculate
        )

        report_data = {
            "type": "projects_breakdown",
            "project_breakdown": project_breakdown,
            "filter": {
                "timeframe": timeframe,
                "start_date": parsed_start_date.isoformat()
                if parsed_start_date
                else None,
                "end_date": parsed_end_date.isoformat() if parsed_end_date else None,
            },
            "recalculated": force_recalculate,
        }

        if output_format == "table":
            self._display_projects_breakdown_table(project_breakdown)
        elif output_format == "json":
            return self._format_projects_breakdown_json(
                project_breakdown, force_recalculate
            )
        elif output_format == "csv":
            return self._format_projects_breakdown_csv(
                project_breakdown, force_recalculate
            )

        return report_data

    # Table display methods
    def _display_single_session_table(
        self,
        session: SessionData,
        stats: Dict[str, Any],
        health: Dict[str, Any],
        force_recalculate: bool = False,
    ):
        """Display single session report as table."""
        # Create session details table
        table = self.table_formatter.create_session_table(
            session, self.analyzer.pricing_data, force_recalculate
        )
        self.console.print(table)

        # Create summary panel
        summary_panel = self.table_formatter.create_summary_panel(
            [session], self.analyzer.pricing_data, force_recalculate
        )
        self.console.print(summary_panel)

        # Show health warnings if any
        if health["warnings"]:
            warning_text = "\n".join(
                [f"⚠️  {warning}" for warning in health["warnings"]]
            )
            warning_panel = Panel(
                warning_text, title="Warnings", border_style="status.warning"
            )
            self.console.print(warning_panel)

    def _display_sessions_summary_table(
        self,
        sessions: List[SessionData],
        summary: Dict[str, Any],
        force_recalculate: bool = False,
    ):
        """Display sessions summary as table."""
        table = self.table_formatter.create_sessions_table(
            sessions, self.analyzer.pricing_data, force_recalculate
        )
        self.console.print(table)

        summary_panel = self.table_formatter.create_summary_panel(
            sessions, self.analyzer.pricing_data, force_recalculate
        )
        self.console.print(summary_panel)

    def _display_workflow_sessions_table(
        self,
        sessions: List[SessionData],
        summary: Dict[str, Any],
        force_recalculate: bool = False,
    ):
        """Display sessions grouped by workflow using semantic theme tags."""
        from rich.table import Table

        # Group sessions into workflows
        grouper = SessionGrouper()
        workflows = grouper.group_sessions(sessions)

        table = Table(
            title="Session Workflows",
            show_header=True,
            header_style="table.header",
            title_style="table.title",
        )

        table.add_column("Started", style="table.row.time", no_wrap=True)
        table.add_column(
            "Session / Workflow", style="table.row.main", no_wrap=False, max_width=45
        )
        table.add_column(
            "Project", style="table.row.project", no_wrap=True, max_width=15
        )
        table.add_column("Provider", style="table.row.model", no_wrap=True, max_width=15)
        table.add_column("Model", style="table.row.model", no_wrap=True, max_width=25)
        table.add_column(
            "Agent", justify="center", style="table.row.model", no_wrap=True
        )
        table.add_column("Interactions", justify="right", style="status.success")
        table.add_column("Tokens", justify="right", style="table.row.tokens")
        table.add_column("Cost", justify="right", style="table.row.cost")

        # Display oldest first so most recent is at the bottom (matching --no-group behavior)
        for workflow in reversed(workflows):
            workflow_cost = workflow.calculate_total_cost(
                self.analyzer.pricing_data, force_recalculate
            )

            # Workflow header row (if has sub-agents, show as group)
            if workflow.has_sub_agents:
                # Show workflow summary row
                title = workflow.display_title
                if len(title) > 40:
                    title = title[:37] + "..."

                # Get start time for workflow
                start_time = (
                    workflow.start_time.strftime("%Y-%m-%d %H:%M")
                    if workflow.start_time
                    else "N/A"
                )

                # Get models from all sessions in workflow
                all_models = []
                for s in workflow.all_sessions:
                    all_models.extend(s.models_used)
                unique_models = sorted(set(all_models))
                if len(unique_models) == 1:
                    display_model = unique_models[0]
                else:
                    display_model = f"{unique_models[0]}+{len(unique_models) - 1}"

                provider, model = FileProcessor.split_provider_model(display_model)

                table.add_row(
                    f"[table.row.time]{start_time}[/table.row.time]",
                    f"[table.row.main]{title}[/table.row.main]",
                    f"[table.row.project]{workflow.project_name[:15]}[/table.row.project]",
                    f"[table.row.model]{provider}[/table.row.model]",
                    f"[table.row.model]{model}[/table.row.model]",
                    f"[table.row.model]+{workflow.sub_agent_count}[/table.row.model]",
                    f"[status.success]{sum(s.interaction_count for s in workflow.all_sessions)}[/status.success]",
                    f"[table.row.tokens]{workflow.total_tokens.total:,}[/table.row.tokens]",
                    f"[table.row.cost]{self._fmt_cost(workflow_cost)}[/table.row.cost]",
                    style="table.row.main",
                )

                # Main session row
                main = workflow.main_session
                main_cost = main.calculate_total_cost(
                    self.analyzer.pricing_data, force_recalculate
                )
                main_title = main.display_title
                if len(main_title) > 38:
                    main_title = main_title[:35] + "..."

                # Get model for main session
                main_models = main.models_used
                if len(main_models) == 1:
                    display_model = main_models[0]
                elif len(main_models) > 1:
                    display_model = f"{main_models[0]}+{len(main_models) - 1}"
                else:
                    display_model = "-"

                provider, model = FileProcessor.split_provider_model(display_model)

                table.add_row(
                    "",  # No separate start time for sub-rows
                    f"  ├─ {main_title}",
                    "",
                    provider,
                    model,
                    main.agent or "main",
                    f"{main.interaction_count}",
                    f"{main.total_tokens.total:,}",
                    self._fmt_cost(main_cost),
                    style="table.row.dim",
                )

                # Sub-agent session rows
                for i, sub in enumerate(workflow.sub_agent_sessions):
                    is_last = i == len(workflow.sub_agent_sessions) - 1
                    prefix = "  └─" if is_last else "  ├─"
                    sub_cost = sub.calculate_total_cost(
                        self.analyzer.pricing_data, force_recalculate
                    )
                    sub_title = sub.display_title
                    if len(sub_title) > 38:
                        sub_title = sub_title[:35] + "..."

                    # Get model for sub-agent session
                    sub_models = sub.models_used
                    if len(sub_models) == 1:
                        display_model = sub_models[0]
                    elif len(sub_models) > 1:
                        display_model = f"{sub_models[0]}+{len(sub_models) - 1}"
                    else:
                        display_model = "-"

                    provider, model = FileProcessor.split_provider_model(display_model)

                    table.add_row(
                        "",  # No separate start time for sub-rows
                        f"{prefix} {sub_title}",
                        "",
                        provider,
                        model,
                        sub.agent or "sub",
                        f"{sub.interaction_count}",
                        f"{sub.total_tokens.total:,}",
                        self._fmt_cost(sub_cost),
                        style="table.row.dim",
                    )
            else:
                # Single session (no sub-agents)
                main = workflow.main_session
                main_cost = main.calculate_total_cost(
                    self.analyzer.pricing_data, force_recalculate
                )
                title = main.display_title
                if len(title) > 42:
                    title = title[:39] + "..."

                # Get start time for single session
                start_time = (
                    main.start_time.strftime("%Y-%m-%d %H:%M")
                    if main.start_time
                    else "N/A"
                )

                # Get model for single session
                main_models = main.models_used
                if len(main_models) == 1:
                    display_model = main_models[0]
                elif len(main_models) > 1:
                    display_model = f"{main_models[0]}+{len(main_models) - 1}"
                else:
                    display_model = "-"

                provider, model = FileProcessor.split_provider_model(display_model)

                table.add_row(
                    start_time,
                    title,
                    main.project_name[:15],
                    provider,
                    model,
                    main.agent or "-",
                    f"{main.interaction_count}",
                    f"{main.total_tokens.total:,}",
                    self._fmt_cost(main_cost),
                )

        self.console.print(table)

        # Summary panel
        total_workflows = len(workflows)
        total_with_subs = sum(1 for w in workflows if w.has_sub_agents)
        summary_panel = self.table_formatter.create_summary_panel(
            sessions, self.analyzer.pricing_data, force_recalculate
        )
        self.console.print(summary_panel)

        # Additional workflow info
        if total_with_subs > 0:
            from rich.panel import Panel

            workflow_info = (
                f"{total_workflows} workflows ({total_with_subs} with sub-agents)"
            )
            self.console.print(
                Panel(
                    workflow_info, title="Workflow Summary", border_style="table.header"
                )
            )

    def _display_daily_breakdown_table(
        self,
        daily_usage: List[DailyUsage],
        breakdown: bool = False,
        force_recalculate: bool = False,
    ):
        """Display daily breakdown as table using semantic theme tags."""
        if breakdown:
            from rich.table import Table

            table = Table(
                title="Daily Usage Breakdown",
                show_header=True,
                header_style="table.header",
                title_style="table.title",
            )

            table.add_column("Date / Model", style="table.row.time", no_wrap=True)
            table.add_column("Sessions", justify="right", style="status.success")
            table.add_column("Interactions", justify="right", style="status.success")
            table.add_column("Input", justify="right", style="table.row.tokens")
            table.add_column("Output", justify="right", style="table.row.tokens")
            table.add_column("Cache Read", justify="right", style="table.row.tokens")
            table.add_column("Cache Write", justify="right", style="table.row.tokens")
            table.add_column("Total Tokens", justify="right", style="table.row.tokens")
            table.add_column("Cost", justify="right", style="table.row.cost")

            for day in daily_usage:
                day_cost = day.calculate_total_cost(
                    self.analyzer.pricing_data, force_recalculate
                )
                table.add_row(
                    day.date.strftime("%Y-%m-%d"),
                    f"{len(day.sessions)}",
                    f"{day.total_interactions}",
                    f"{day.total_tokens.input:,}",
                    f"{day.total_tokens.output:,}",
                    f"{day.total_tokens.cache_read:,}",
                    f"{day.total_tokens.cache_write:,}",
                    f"{day.total_tokens.total:,}",
                    self._fmt_cost(day_cost),
                )

                model_breakdown = self._get_model_breakdown_for_sessions(
                    day.sessions, force_recalculate
                )
                for row in model_breakdown:
                    table.add_row(
                        f"  ↳ {row['model']}",
                        f"{row['sessions']}",
                        f"{row['interactions']}",
                        f"{row['input_tokens']:,}",
                        f"{row['output_tokens']:,}",
                        f"{row['cache_read']:,}",
                        f"{row['cache_write']:,}",
                        f"{row['tokens']:,}",
                        self._fmt_cost(row["cost"]),
                        style="table.row.dim",
                    )

            self.console.print(table)
        else:
            table = self.table_formatter.create_daily_table(
                daily_usage, self.analyzer.pricing_data, force_recalculate
            )
            self.console.print(table)

    def _display_weekly_breakdown_table(
        self,
        weekly_usage: List[WeeklyUsage],
        breakdown: bool = False,
        week_start_day: int = 0,
        force_recalculate: bool = False,
    ):
        """Display weekly breakdown as table using semantic theme tags."""
        from rich.table import Table

        from ..utils.time_utils import WEEKDAY_NAMES, TimeUtils

        title = "Weekly Usage Breakdown"
        if week_start_day != 0:
            day_name = WEEKDAY_NAMES[week_start_day]
            title += f" (weeks start on {day_name})"

        table = Table(
            title=title,
            show_header=True,
            header_style="table.header",
            title_style="table.title",
        )

        table.add_column("Week", style="table.row.time", no_wrap=True)
        table.add_column("Date Range", style="table.row.time", no_wrap=False)
        table.add_column("Sessions", justify="right", style="status.success")
        table.add_column("Interactions", justify="right", style="status.success")
        table.add_column("Total Tokens", justify="right", style="table.row.tokens")
        table.add_column("Cost", justify="right", style="table.row.cost")

        for week in weekly_usage:
            week_cost = week.calculate_total_cost(
                self.analyzer.pricing_data, force_recalculate
            )
            week_label = f"{week.year}-W{week.week:02d}"
            date_range = TimeUtils.format_week_range(week.start_date, week.end_date)

            table.add_row(
                week_label,
                date_range,
                f"{week.total_sessions}",
                f"{week.total_interactions}",
                f"{week.total_tokens.total:,}",
                self._fmt_cost(week_cost),
            )

            if breakdown:
                week_sessions = []
                for day in week.daily_usage:
                    week_sessions.extend(day.sessions)

                model_breakdown = self._get_model_breakdown_for_sessions(
                    week_sessions, force_recalculate
                )
                for row in model_breakdown:
                    table.add_row(
                        "",
                        f"  ↳ {row['model']}",
                        f"{row['sessions']}",
                        f"{row['interactions']}",
                        f"{row['tokens']:,}",
                        self._fmt_cost(row["cost"]),
                        style="table.row.dim",
                    )

        self.console.print(table)

    def _display_monthly_breakdown_table(
        self,
        monthly_usage: List[MonthlyUsage],
        breakdown: bool = False,
        force_recalculate: bool = False,
    ):
        """Display monthly breakdown as table using semantic theme tags."""
        from rich.table import Table

        table = Table(
            title="Monthly Usage Breakdown",
            show_header=True,
            header_style="table.header",
            title_style="table.title",
        )

        table.add_column("Month / Model", style="table.row.time", no_wrap=True)
        table.add_column("Sessions", justify="right", style="status.success")
        table.add_column("Interactions", justify="right", style="status.success")
        table.add_column("Total Tokens", justify="right", style="table.row.tokens")
        table.add_column("Cost", justify="right", style="table.row.cost")

        for month in monthly_usage:
            month_cost = month.calculate_total_cost(
                self.analyzer.pricing_data, force_recalculate
            )
            table.add_row(
                f"{month.year}-{month.month:02d}",
                f"{month.total_sessions}",
                f"{month.total_interactions}",
                f"{month.total_tokens.total:,}",
                self._fmt_cost(month_cost),
            )

            if breakdown:
                month_sessions = []
                for week in month.weekly_usage:
                    for day in week.daily_usage:
                        month_sessions.extend(day.sessions)

                model_breakdown = self._get_model_breakdown_for_sessions(
                    month_sessions, force_recalculate
                )
                for row in model_breakdown:
                    table.add_row(
                        f"  ↳ {row['model']}",
                        f"{row['sessions']}",
                        f"{row['interactions']}",
                        f"{row['tokens']:,}",
                        self._fmt_cost(row["cost"]),
                        style="table.row.dim",
                    )

        self.console.print(table)

    def _display_models_breakdown_table(self, model_breakdown: ModelBreakdownReport):
        """Display models breakdown as table."""
        table = self.table_formatter.create_model_breakdown_table(
            model_breakdown.model_stats
        )
        self.console.print(table)

    def _display_projects_breakdown_table(
        self, project_breakdown: ProjectBreakdownReport
    ):
        """Display projects breakdown as table using semantic theme tags."""
        from rich.table import Table

        table = Table(
            title="Project Usage Breakdown",
            show_header=True,
            header_style="table.header",
            title_style="table.title",
        )

        table.add_column("Project", style="table.row.project")
        table.add_column("Sessions", justify="right", style="status.success")
        table.add_column("Interactions", justify="right", style="status.success")
        table.add_column("Total Tokens", justify="right", style="table.row.tokens")
        table.add_column("Cost", justify="right", style="table.row.cost")
        table.add_column("Models Used", style="table.row.model")

        for project in project_breakdown.project_stats:
            # Truncate models list if too long
            models_display = ", ".join(project.models_used)
            if len(models_display) > 40:
                models_display = models_display[:37] + "..."

            table.add_row(
                project.project_name,
                f"{project.total_sessions}",
                f"{project.total_interactions}",
                f"{project.total_tokens.total:,}",
                self._fmt_cost(project.total_cost),
                models_display,
            )

        self.console.print(table)

        # Add summary
        from rich.panel import Panel

        summary_text = (
            f"[metric.important]Total:[/metric.important] [metric.value]{len(project_breakdown.project_stats)}[/metric.value] projects, "
            f"[metric.value]{sum(p.total_sessions for p in project_breakdown.project_stats)}[/metric.value] sessions, "
            f"[metric.value]{sum(p.total_interactions for p in project_breakdown.project_stats)}[/metric.value] interactions, "
            f"[metric.tokens]{project_breakdown.total_tokens.total:,}[/metric.tokens] tokens, "
            f"[metric.cost]{self._fmt_cost(project_breakdown.total_cost)}[/metric.cost]"
        )
        summary_panel = Panel(
            summary_text, title="Summary", border_style="status.success"
        )
        self.console.print(summary_panel)

    # ... Rest of formatting methods (JSON, CSV) remain unchanged ...

    def _format_single_session_json(
        self,
        session: SessionData,
        stats: Dict[str, Any],
        health: Dict[str, Any],
        force_recalculate: bool = False,
    ) -> Dict[str, Any]:
        """Format single session data as JSON."""
        return {
            "session_id": session.session_id,
            "session_title": session.session_title,
            "project_name": session.project_name,
            "statistics": {
                "interaction_count": stats["interaction_count"],
                "total_tokens": stats["total_tokens"].model_dump(),
                "total_cost": float(stats["total_cost"]),
                "models_used": stats["models_used"],
            },
            "health": health,
            "recalculated": force_recalculate,
            "interactions": [
                {
                    "file_name": file.file_name,
                    "model_id": file.model_id,
                    "tokens": file.tokens.model_dump(),
                    "cost": float(
                        file.calculate_cost(
                            self.analyzer.pricing_data, force_recalculate
                        )
                    ),
                }
                for file in session.files
            ],
        }

    def _format_sessions_summary_json(
        self,
        sessions: List[SessionData],
        summary: Dict[str, Any],
        force_recalculate: bool = False,
    ) -> Dict[str, Any]:
        """Format sessions summary as JSON."""
        return {
            "summary": {
                "total_sessions": summary["total_sessions"],
                "total_interactions": summary["total_interactions"],
                "total_tokens": summary["total_tokens"].model_dump(),
                "total_cost": float(summary["total_cost"]),
                "models_used": summary["models_used"],
                "date_range": summary["date_range"],
            },
            "recalculated": force_recalculate,
            "sessions": [
                {
                    "session_id": session.session_id,
                    "session_title": session.session_title,
                    "project_name": session.project_name,
                    "interaction_count": session.interaction_count,
                    "total_tokens": session.total_tokens.model_dump(),
                    "total_cost": float(
                        session.calculate_total_cost(
                            self.analyzer.pricing_data, force_recalculate
                        )
                    ),
                    "models_used": session.models_used,
                    "start_time": session.start_time.isoformat()
                    if session.start_time
                    else None,
                    "end_time": session.end_time.isoformat()
                    if session.end_time
                    else None,
                }
                for session in sessions
            ],
        }

    def _format_daily_breakdown_json(
        self, daily_usage: List[DailyUsage], force_recalculate: bool = False
    ) -> Dict[str, Any]:
        """Format daily breakdown as JSON."""
        return {
            "daily_breakdown": [
                {
                    "date": day.date.isoformat(),
                    "sessions": len(day.sessions),
                    "interactions": day.total_interactions,
                    "tokens": day.total_tokens.model_dump(),
                    "cost": float(
                        day.calculate_total_cost(
                            self.analyzer.pricing_data, force_recalculate
                        )
                    ),
                    "models_used": day.models_used,
                }
                for day in daily_usage
            ],
            "recalculated": force_recalculate,
        }

    def _format_weekly_breakdown_json(
        self, weekly_usage: List[WeeklyUsage], force_recalculate: bool = False
    ) -> Dict[str, Any]:
        """Format weekly breakdown as JSON."""
        return {
            "weekly_breakdown": [
                {
                    "year": week.year,
                    "week": week.week,
                    "start_date": week.start_date.isoformat(),
                    "end_date": week.end_date.isoformat(),
                    "sessions": week.total_sessions,
                    "interactions": week.total_interactions,
                    "tokens": week.total_tokens.model_dump(),
                    "cost": float(
                        week.calculate_total_cost(
                            self.analyzer.pricing_data, force_recalculate
                        )
                    ),
                }
                for week in weekly_usage
            ],
            "recalculated": force_recalculate,
        }

    def _format_monthly_breakdown_json(
        self, monthly_usage: List[MonthlyUsage], force_recalculate: bool = False
    ) -> Dict[str, Any]:
        """Format monthly breakdown as JSON."""
        return {
            "monthly_breakdown": [
                {
                    "year": month.year,
                    "month": month.month,
                    "sessions": month.total_sessions,
                    "interactions": month.total_interactions,
                    "tokens": month.total_tokens.model_dump(),
                    "cost": float(
                        month.calculate_total_cost(
                            self.analyzer.pricing_data, force_recalculate
                        )
                    ),
                }
                for month in monthly_usage
            ],
            "recalculated": force_recalculate,
        }

    def _format_models_breakdown_json(
        self, model_breakdown: ModelBreakdownReport, force_recalculate: bool = False
    ) -> Dict[str, Any]:
        """Format models breakdown as JSON."""
        return {
            "timeframe": model_breakdown.timeframe,
            "start_date": model_breakdown.start_date.isoformat()
            if model_breakdown.start_date
            else None,
            "end_date": model_breakdown.end_date.isoformat()
            if model_breakdown.end_date
            else None,
            "total_cost": float(model_breakdown.total_cost),
            "total_tokens": model_breakdown.total_tokens.model_dump(),
            "recalculated": force_recalculate,
            "models": [
                {
                    "model_name": model.display_model,
                    "sessions": model.total_sessions,
                    "interactions": model.total_interactions,
                    "tokens": model.total_tokens.model_dump(),
                    "cost": float(model.total_cost),
                    "p50_output_rate": model.p50_output_rate,
                    "first_used": model.first_used.isoformat()
                    if model.first_used
                    else None,
                    "last_used": model.last_used.isoformat()
                    if model.last_used
                    else None,
                }
                for model in model_breakdown.model_stats
            ],
        }

    def _format_single_session_csv(
        self,
        session: SessionData,
        stats: Dict[str, Any],
        force_recalculate: bool = False,
    ) -> List[Dict[str, Any]]:
        """Format single session data for CSV export."""
        return [
            {
                "session_id": session.session_id,
                "session_title": session.session_title,
                "project_name": session.project_name,
                "file_name": file.file_name,
                "model_id": file.model_id,
                "input_tokens": file.tokens.input,
                "output_tokens": file.tokens.output,
                "cache_write_tokens": file.tokens.cache_write,
                "cache_read_tokens": file.tokens.cache_read,
                "total_tokens": file.tokens.total,
                "cost": float(
                    file.calculate_cost(self.analyzer.pricing_data, force_recalculate)
                ),
                "duration_ms": file.time_data.duration_ms if file.time_data else None,
            }
            for file in session.files
        ]

    def _format_sessions_summary_csv(
        self, sessions: List[SessionData], force_recalculate: bool = False
    ) -> List[Dict[str, Any]]:
        """Format sessions summary for CSV export."""
        rows = []
        for session in sessions:
            model_breakdown = session.get_model_breakdown(
                self.analyzer.pricing_data, force_recalculate
            )
            for model, stats in model_breakdown.items():
                rows.append(
                    {
                        "session_id": session.session_id,
                        "session_title": session.session_title,
                        "project_name": session.project_name,
                        "start_time": session.start_time.isoformat()
                        if session.start_time
                        else None,
                        "duration_ms": session.duration_ms,
                        "model": model,
                        "interactions": stats["files"],
                        "input_tokens": stats["tokens"].input,
                        "output_tokens": stats["tokens"].output,
                        "cache_write_tokens": stats["tokens"].cache_write,
                        "cache_read_tokens": stats["tokens"].cache_read,
                        "total_tokens": stats["tokens"].total,
                        "cost": float(stats["cost"]),
                    }
                )
        return rows

    def _format_daily_breakdown_csv(
        self, daily_usage: List[DailyUsage], force_recalculate: bool = False
    ) -> List[Dict[str, Any]]:
        """Format daily breakdown for CSV export."""
        return [
            {
                "date": day.date.isoformat(),
                "sessions": len(day.sessions),
                "interactions": day.total_interactions,
                "input_tokens": day.total_tokens.input,
                "output_tokens": day.total_tokens.output,
                "cache_write_tokens": day.total_tokens.cache_write,
                "cache_read_tokens": day.total_tokens.cache_read,
                "total_tokens": day.total_tokens.total,
                "cost": float(
                    day.calculate_total_cost(
                        self.analyzer.pricing_data, force_recalculate
                    )
                ),
                "models_used": ", ".join(day.models_used),
            }
            for day in daily_usage
        ]

    def _format_weekly_breakdown_csv(
        self, weekly_usage: List[WeeklyUsage], force_recalculate: bool = False
    ) -> List[Dict[str, Any]]:
        """Format weekly breakdown for CSV export."""
        return [
            {
                "year": week.year,
                "week": week.week,
                "start_date": week.start_date.isoformat(),
                "end_date": week.end_date.isoformat(),
                "sessions": week.total_sessions,
                "interactions": week.total_interactions,
                "input_tokens": week.total_tokens.input,
                "output_tokens": week.total_tokens.output,
                "cache_write_tokens": week.total_tokens.cache_write,
                "cache_read_tokens": week.total_tokens.cache_read,
                "total_tokens": week.total_tokens.total,
                "cost": float(
                    week.calculate_total_cost(
                        self.analyzer.pricing_data, force_recalculate
                    )
                ),
            }
            for week in weekly_usage
        ]

    def _format_monthly_breakdown_csv(
        self, monthly_usage: List[MonthlyUsage], force_recalculate: bool = False
    ) -> List[Dict[str, Any]]:
        """Format monthly breakdown for CSV export."""
        return [
            {
                "year": month.year,
                "month": month.month,
                "sessions": month.total_sessions,
                "interactions": month.total_interactions,
                "input_tokens": month.total_tokens.input,
                "output_tokens": month.total_tokens.output,
                "cache_write_tokens": month.total_tokens.cache_write,
                "cache_read_tokens": month.total_tokens.cache_read,
                "total_tokens": month.total_tokens.total,
                "cost": float(
                    month.calculate_total_cost(
                        self.analyzer.pricing_data, force_recalculate
                    )
                ),
            }
            for month in monthly_usage
        ]

    def _format_models_breakdown_csv(
        self, model_breakdown: ModelBreakdownReport, force_recalculate: bool = False
    ) -> List[Dict[str, Any]]:
        """Format models breakdown for CSV export."""
        return [
            {
                "model_name": model.display_model,
                "sessions": model.total_sessions,
                "interactions": model.total_interactions,
                "input_tokens": model.total_tokens.input,
                "output_tokens": model.total_tokens.output,
                "cache_write_tokens": model.total_tokens.cache_write,
                "cache_read_tokens": model.total_tokens.cache_read,
                "total_tokens": model.total_tokens.total,
                "cost": float(model.total_cost),
                "p50_output_rate": model.p50_output_rate,
                "first_used": model.first_used.isoformat()
                if model.first_used
                else None,
                "last_used": model.last_used.isoformat() if model.last_used else None,
                "recalculated": force_recalculate,
            }
            for model in model_breakdown.model_stats
        ]

    def _format_projects_breakdown_json(
        self, project_breakdown: ProjectBreakdownReport, force_recalculate: bool = False
    ) -> Dict[str, Any]:
        """Format projects breakdown as JSON."""
        return {
            "timeframe": project_breakdown.timeframe,
            "start_date": project_breakdown.start_date.isoformat()
            if project_breakdown.start_date
            else None,
            "end_date": project_breakdown.end_date.isoformat()
            if project_breakdown.end_date
            else None,
            "total_cost": float(project_breakdown.total_cost),
            "total_tokens": project_breakdown.total_tokens.model_dump(),
            "recalculated": force_recalculate,
            "projects": [
                {
                    "project_name": project.project_name,
                    "sessions": project.total_sessions,
                    "interactions": project.total_interactions,
                    "tokens": project.total_tokens.model_dump(),
                    "cost": float(project.total_cost),
                    "models_used": project.models_used,
                    "first_activity": project.first_activity.isoformat()
                    if project.first_activity
                    else None,
                    "last_activity": project.last_activity.isoformat()
                    if project.last_activity
                    else None,
                }
                for project in project_breakdown.project_stats
            ],
        }

    def _format_projects_breakdown_csv(
        self, project_breakdown: ProjectBreakdownReport, force_recalculate: bool = False
    ) -> List[Dict[str, Any]]:
        """Format projects breakdown for CSV export."""
        return [
            {
                "project_name": project.project_name,
                "sessions": project.total_sessions,
                "interactions": project.total_interactions,
                "input_tokens": project.total_tokens.input,
                "output_tokens": project.total_tokens.output,
                "cache_write_tokens": project.total_tokens.cache_write,
                "cache_read_tokens": project.total_tokens.cache_read,
                "total_tokens": project.total_tokens.total,
                "cost": float(project.total_cost),
                "models_used": ", ".join(project.models_used),
                "first_activity": project.first_activity.isoformat()
                if project.first_activity
                else None,
                "last_activity": project.last_activity.isoformat()
                if project.last_activity
                else None,
                "recalculated": force_recalculate,
            }
            for project in project_breakdown.project_stats
        ]
