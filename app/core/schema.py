"""Schema change detection: adapter expectations + stored baseline of the last accepted file."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.enums import ErrorType, Severity
from app.core.types import Issue, TableProfile


@dataclass
class ExpectedSchema:
    role: str
    required_columns: list[str] = field(default_factory=list)
    expected_column_count: int | None = None
    min_rows: int = 1
    # Columns whose inferred type is checked against the baseline (others are free text that may legitimately vary).
    typed_columns: list[str] | None = None


ROW_COUNT_CHANGE_THRESHOLD = 0.5


def _issue(message: str, role: str, **details: Any) -> Issue:
    return Issue(
        error_type=ErrorType.SCHEMA_CHANGED,
        severity=Severity.ERROR,
        message=message,
        stage="schema_check",
        details={"role": role, **details},
    )


def check_expected(profile: TableProfile, expected: ExpectedSchema, *, truncated: bool) -> list[Issue]:
    issues: list[Issue] = []
    if expected.expected_column_count is not None and len(profile.columns) != expected.expected_column_count:
        issues.append(
            _issue(
                f"[{profile.role}] Expected columns: {expected.expected_column_count} – Actual columns: {len(profile.columns)}",
                profile.role,
                expected=expected.expected_column_count,
                actual=len(profile.columns),
                columns=profile.columns,
            )
        )
    missing = [c for c in expected.required_columns if c not in profile.columns]
    if missing:
        issues.append(
            _issue(
                f"[{profile.role}] Expected column(s) not found: {missing}",
                profile.role,
                missing=missing,
                actual_columns=profile.columns,
            )
        )
    if not truncated and profile.row_count < expected.min_rows:
        issues.append(
            _issue(
                f"[{profile.role}] Only {profile.row_count} rows found (minimum expected {expected.min_rows})",
                profile.role,
                row_count=profile.row_count,
            )
        )
    return issues


def compare_with_baseline(
    profile: TableProfile, baseline: dict[str, Any] | None, *, truncated: bool, typed_columns: list[str] | None = None
) -> list[Issue]:
    """Compare the parsed profile with the last accepted one (columns, types, row count)."""
    if not baseline:
        return []
    issues: list[Issue] = []
    base_cols: list[str] = baseline.get("columns") or []
    if base_cols != profile.columns:
        added = [c for c in profile.columns if c not in base_cols]
        removed = [c for c in base_cols if c not in profile.columns]
        msg = f"[{profile.role}] Column set changed: expected {len(base_cols)} columns, found {len(profile.columns)}"
        if added:
            msg += f"; added {added}"
        if removed:
            msg += f"; removed {removed}"
        if not added and not removed:
            msg += "; column order changed"
        issues.append(_issue(msg, profile.role, added=added, removed=removed, baseline_columns=base_cols))
    base_types: dict[str, str] = baseline.get("column_types") or {}
    for col in typed_columns if typed_columns is not None else profile.columns:
        old, new = base_types.get(col), profile.column_types.get(col)
        if old and new and "empty" not in (old, new) and old != new:
            issues.append(
                _issue(
                    f"[{profile.role}] Type of column '{col}' changed from {old} to {new}",
                    profile.role,
                    column=col,
                    old_type=old,
                    new_type=new,
                )
            )
    base_rows = baseline.get("row_count")
    if not truncated and base_rows:
        change = abs(profile.row_count - base_rows) / base_rows
        if change > ROW_COUNT_CHANGE_THRESHOLD:
            issues.append(
                _issue(
                    f"[{profile.role}] Row count changed by {change:.0%} ({base_rows} → {profile.row_count})",
                    profile.role,
                    baseline_rows=base_rows,
                    rows=profile.row_count,
                )
            )
    return issues
