from __future__ import annotations

import typing as t
from collections.abc import Collection, Mapping

from sqlglot import exp
from sqlglot.errors import UnsupportedError
from sqlglot.optimizer.scope import Scope, traverse_scope

if t.TYPE_CHECKING:
    from sqlglot.dialects.dialect import DialectType


class TenantFilterError(UnsupportedError):
    """Raised when a query cannot be safely rewritten with a tenant access filter."""


# Table reference features whose semantics are not guaranteed to survive being wrapped in a
# filtering subquery, so their presence on a protected table is rejected outright.
_UNSUPPORTED_TABLE_ARGS = (
    "laterals",
    "joins",
    "pivots",
    "hints",
    "system_time",
    "version",
    "format",
    "pattern",
    "ordinality",
    "when",
    "only",
    "partition",
    "changes",
    "rows_from",
    "sample",
    "indexed",
)


def add_tenant_filter(
    expression: str | exp.Expr,
    tables: Mapping[str, str] | Collection[str],
    tenant: t.Any,
    column: str = "tenant_id",
    dialect: DialectType = None,
    copy: bool = True,
) -> exp.Expr:
    """
    Rewrite a SELECT query so that every reference to a protected real table only sees rows
    belonging to a single tenant.

    Each protected table reference is wrapped in a filtering subquery, e.g.
    ``orders AS o`` becomes ``(SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS o``.
    Wrapping (instead of adding WHERE predicates) preserves the null-side rows of outer
    joins while still restricting the protected side to the current tenant.

    Only SELECT queries (including set operations, subqueries and CTEs) are supported.
    References that resolve to a CTE or derived table rather than a real table are left
    untouched, so a CTE that shadows a protected table name is not filtered. Anything that
    cannot be rewritten safely raises a ``TenantFilterError`` instead of being silently
    skipped.

    Args:
        expression: the SQL string or expression to rewrite.
        tables: the real tables to protect, either a mapping of table name to tenant column
            name or a collection of table names that all use ``column``. Names may be
            qualified (``"db.orders"``); unqualified names match the table's last part.
        tenant: the tenant value to filter on. Plain Python values become literals, an
            ``exp.Expr`` (e.g. ``exp.Placeholder``) is inlined as-is.
        column: the tenant column for every table when ``tables`` is not a mapping.
        dialect: the dialect used to parse ``expression`` if it is a string.
        copy: if ``False``, mutate the passed-in expression in place.

    Returns:
        The rewritten expression.

    Example:
        >>> import sqlglot
        >>> sqlglot.add_tenant_filter(
        ...     "SELECT * FROM orders AS o LEFT JOIN customers AS c ON c.id = o.customer_id",
        ...     tables={"orders": "tenant_id", "customers": "tenant_id"},
        ...     tenant="acme",
        ... ).sql()
        "SELECT * FROM (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS o LEFT JOIN (SELECT * FROM customers WHERE customers.tenant_id = 'acme') AS c ON c.id = o.customer_id"
    """
    if isinstance(expression, str):
        expression = exp.maybe_parse(expression, dialect=dialect)
    elif copy:
        expression = expression.copy()

    if not isinstance(expression, (exp.Select, exp.SetOperation)):
        raise TenantFilterError(
            f"Tenant filters can only be applied to SELECT queries, "
            f"got {type(expression).__name__}"
        )

    protected = _protected_tables(tables, column)
    tenant_expr = tenant if isinstance(tenant, exp.Expr) else exp.convert(tenant)

    for scope in traverse_scope(expression):
        for table in scope.tables:
            source = scope.sources.get(table.alias_or_name)

            if source is not table:
                # A Scope source means the reference resolves to a CTE or derived table
                # shadowing the real table name; anything else (e.g. a duplicate,
                # unaliased reference to the same table) is ambiguous and rejected.
                if not isinstance(source, Scope) and _tenant_column(table, protected):
                    raise TenantFilterError(
                        f"Cannot safely apply a tenant filter: source "
                        f"'{table.alias_or_name}' is used more than once without an alias"
                    )
                continue

            tenant_column = _tenant_column(table, protected)
            if tenant_column:
                _wrap_table(table, tenant_column, tenant_expr)

    return expression


def _protected_tables(
    tables: Mapping[str, str] | Collection[str], column: str
) -> dict[tuple[str, ...], str]:
    items: t.Iterable[tuple[str, str]]
    if isinstance(tables, Mapping):
        items = tables.items()
    else:
        items = ((name, column) for name in tables)

    protected = {}
    for name, tenant_column in items:
        table = exp.to_table(name)
        if not table.name:
            raise TenantFilterError(f"Invalid protected table name: {name!r}")
        protected[tuple(_part_key(part) for part in table.parts)] = tenant_column

    return protected


def _part_key(part: exp.Expr) -> str:
    # Quoted identifiers are case-sensitive, unquoted ones are not
    name = part.name
    return name if isinstance(part, exp.Identifier) and part.quoted else name.lower()


def _tenant_column(table: exp.Table, protected: dict[tuple[str, ...], str]) -> str | None:
    parts = tuple(_part_key(part) for part in table.parts)

    for key, tenant_column in protected.items():
        if len(key) <= len(parts) and parts[-len(key) :] == key:
            return tenant_column

    return None


def _wrap_table(table: exp.Table, tenant_column: str, tenant_expr: exp.Expr) -> None:
    unsupported = [arg for arg in _UNSUPPORTED_TABLE_ARGS if table.args.get(arg) is not None]
    if unsupported:
        raise TenantFilterError(
            f"Cannot safely apply a tenant filter to '{table.sql()}': "
            f"unsupported table feature(s): {', '.join(unsupported)}"
        )

    if isinstance(table.parent, exp.Final) or not isinstance(table.this, exp.Identifier):
        raise TenantFilterError(f"Cannot safely apply a tenant filter to '{table.sql()}'")

    inner_table = table.copy()
    inner_table.set("alias", None)

    predicate = exp.Column(this=exp.to_identifier(tenant_column), table=inner_table.this.copy()).eq(
        tenant_expr.copy()
    )

    filtered = exp.select("*").from_(inner_table).where(predicate)

    alias = table.args.get("alias")
    table.replace(
        exp.Subquery(
            this=filtered,
            alias=alias.copy() if alias else exp.TableAlias(this=table.this.copy()),
        )
    )
