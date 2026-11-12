"""Read-only SQL classification for report-style backends.

Replaces naive "starts with SELECT" checks with an AST-based verdict:
each input is parsed with a chosen dialect and every statement is
validated, reporting exactly which statement or subtree is rejected.
"""

from __future__ import annotations

import typing as t

from sqlglot import exp, parse
from sqlglot.errors import SqlglotError

if t.TYPE_CHECKING:
    from sqlglot.dialects.dialect import DialectType


class ReadOnlyViolation(t.NamedTuple):
    """A single reason an input is not read-only."""

    statement: int
    """1-based index of the offending statement, 0 if not tied to one."""

    reason: str
    """Human-readable explanation of why this construct is rejected."""

    sql: str
    """SQL of the offending statement or subtree."""


class ReadOnlyReport(t.NamedTuple):
    """Result of classifying an input as read-only or not."""

    readonly: bool
    """True only when every statement is a verifiably read-only query."""

    violations: t.List[ReadOnlyViolation]
    """All violations found; empty when the input is read-only."""


def _sql(node: exp.Expression, dialect: DialectType = None) -> str:
    try:
        return node.sql(dialect=dialect)
    except Exception:
        return str(node)


def classify_readonly(sql: str, dialect: DialectType = None) -> ReadOnlyReport:
    """Classify `sql` as read-only using `dialect` to parse it.

    A statement is read-only when it parses to a plain query (SELECT,
    UNION, VALUES, ...) with no SELECT ... INTO target, no data-modifying
    CTE, and no DML/DDL/Command node anywhere in its subtree. Comments
    and string literals never affect the verdict, since classification
    operates on the AST rather than the raw text.
    """
    try:
        statements = [
            statement
            for statement in parse(sql, dialect=dialect)
            if statement is not None and not isinstance(statement, exp.Semicolon)
        ]
    except SqlglotError as e:
        return ReadOnlyReport(
            readonly=False,
            violations=[ReadOnlyViolation(0, f"unable to parse SQL: {e}", sql)],
        )

    if not statements:
        return ReadOnlyReport(
            readonly=False,
            violations=[ReadOnlyViolation(0, "no statement found in the input", sql)],
        )

    violations: t.List[ReadOnlyViolation] = []

    if len(statements) > 1:
        violations.append(
            ReadOnlyViolation(
                2,
                f"expected a single statement, found {len(statements)}",
                _sql(statements[1], dialect),
            )
        )

    for index, statement in enumerate(statements, start=1):
        if isinstance(statement, exp.Command):
            violations.append(
                ReadOnlyViolation(
                    index,
                    "statement cannot be verified as read-only (parsed as a raw Command)",
                    _sql(statement, dialect),
                )
            )
            continue

        if not isinstance(statement, (exp.Query, exp.Values)):
            violations.append(
                ReadOnlyViolation(
                    index,
                    f"{type(statement).__name__} is not a read-only query",
                    _sql(statement, dialect),
                )
            )
            continue

        for select in statement.find_all(exp.Select):
            if select.args.get("into"):
                violations.append(
                    ReadOnlyViolation(
                        index,
                        "SELECT ... INTO writes to a new table",
                        _sql(select, dialect),
                    )
                )

        for node in statement.find_all(exp.DML, exp.DDL, exp.Command):
            ancestor = "data-modifying CTE" if node.find_ancestor(exp.CTE) else "write operation"
            violations.append(
                ReadOnlyViolation(
                    index,
                    f"{ancestor} {type(node).__name__} is not allowed in a read-only query",
                    _sql(node, dialect),
                )
            )

    return ReadOnlyReport(readonly=not violations, violations=violations)
