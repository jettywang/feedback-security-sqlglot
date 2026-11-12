from __future__ import annotations

import json
import logging
import typing as t
from dataclasses import dataclass, field

from sqlglot import Schema, exp, maybe_parse
from sqlglot.errors import SqlglotError
from sqlglot.optimizer import Scope, build_scope, find_all_in_scope, normalize_identifiers, qualify
from sqlglot.optimizer.scope import ScopeType
from sqlglot.schema import ensure_schema

if t.TYPE_CHECKING:
    from sqlglot.dialects.dialect import DialectType
    from collections.abc import Iterator, Mapping, Sequence
    from sqlglot._typing import GraphHTMLArgs
    from typing_extensions import Unpack

logger = logging.getLogger("sqlglot")


# Reason used when a `*` projection reaches the lineage graph unexpanded, which
# means the output columns (and therefore their sources) cannot be enumerated.
STAR_NOT_EXPANDED_REASON = (
    "'*' projection was not expanded (schema unavailable or star expansion disabled); "
    "the output columns are unknown"
)


@dataclass(frozen=True)
class SourceRef:
    """A fully-resolved base source of an output column.

    `table` is the full table name and `column` the column within it. For sources
    that are not tables (e.g. a `VALUES` clause or a UDTF), `table` is empty and
    `expression` carries the source's SQL.
    """

    table: str
    column: str
    expression: str = ""


@dataclass(frozen=True)
class UnresolvedSource:
    """A part of a column's lineage that could not be fully resolved.

    Consumers must treat these as unknown, not as "public" columns.
    """

    reason: str
    name: str = ""
    expression: str = ""


@dataclass(frozen=True)
class Node:
    name: str
    expression: exp.Expr
    source: exp.Expr
    downstream: list[Node] = field(default_factory=list)
    source_name: str = ""
    reference_node_name: str = ""

    # Caller-injected per-node data, populated via the `on_node` hook on lineage()
    payload: dict[str, t.Any] = field(default_factory=dict)

    # Why this node's source could not be fully resolved ("" when fully resolved)
    uncertainty_reason: str = ""

    def walk(self) -> Iterator[Node]:
        visited: set[int] = set()
        queue = [self]
        while queue:
            node = queue.pop()
            node_id = id(node)
            if node_id in visited:
                continue
            visited.add(node_id)
            yield node
            queue.extend(reversed(node.downstream))

    @property
    def is_unresolved(self) -> bool:
        """Whether this node's source could not be fully resolved."""
        return bool(self.uncertainty_reason) or isinstance(self.source, exp.Placeholder)

    @property
    def sources(self) -> tuple[SourceRef, ...]:
        """All fully-resolved base sources reachable from this node.

        Unresolved subtrees (e.g. an unexpanded `*` or an ambiguous column) are
        excluded here and reported through `unresolved` instead, so that an
        unknown source is never silently treated as a public field.
        """
        return _collect_refs(self)[0]

    @property
    def unresolved(self) -> tuple[UnresolvedSource, ...]:
        """The parts of this node's lineage that could not be fully resolved."""
        return _collect_refs(self)[1]

    @property
    def requires_confirmation(self) -> bool:
        """Whether any part of this node's lineage needs manual confirmation."""
        return bool(self.unresolved)

    def to_html(self, dialect: DialectType = None, **opts: Unpack[GraphHTMLArgs]) -> GraphHTML:
        nodes = {}
        edges = []

        for node in self.walk():
            if isinstance(node.expression, exp.Table):
                label = f"FROM {node.expression.this}"
                title = f"<pre>SELECT {node.name} FROM {node.expression.this}</pre>"
                group = 1
            else:
                label = node.expression.sql(pretty=True, dialect=dialect)
                source = node.source.transform(
                    lambda n: (
                        exp.Tag(this=n, prefix="<b>", postfix="</b>") if n is node.expression else n
                    ),
                    copy=False,
                ).sql(pretty=True, dialect=dialect)
                title = f"<pre>{source}</pre>"
                group = 0

            node_id = id(node)

            nodes[node_id] = {
                "id": node_id,
                "label": label,
                "title": title,
                "group": group,
            }

            for d in node.downstream:
                edges.append({"from": node_id, "to": id(d)})
        return GraphHTML(nodes, edges, **opts)


@dataclass(frozen=True)
class ColumnLineage:
    """Position-aware source report for a single output column.

    Unlike `lineage()`'s name-keyed dict, this preserves duplicate output names
    by carrying the projection's `position`, and it separates fully-resolved
    `sources` from `unresolved` parts so consumers never mistake an unknown
    source for a public field.
    """

    name: str
    position: int | None
    node: Node
    sources: tuple[SourceRef, ...]
    unresolved: tuple[UnresolvedSource, ...]

    @property
    def requires_confirmation(self) -> bool:
        """Whether any part of this column's lineage needs manual confirmation."""
        return bool(self.unresolved)

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "name": self.name,
            "position": self.position,
            "sources": [
                {"table": s.table, "column": s.column, "expression": s.expression}
                for s in self.sources
            ],
            "unresolved": [
                {"name": u.name, "reason": u.reason, "expression": u.expression}
                for u in self.unresolved
            ],
            "requires_confirmation": self.requires_confirmation,
        }


def _prepare_scope(
    sql: str | exp.Expr,
    schema: dict | Schema,
    sources: Mapping[str, str | exp.Query] | None,
    dialect: DialectType,
    scope: Scope | None,
    copy: bool,
    kwargs: dict[str, t.Any],
) -> tuple[Scope, Schema, exp.Selectable]:
    expression = maybe_parse(sql, copy=copy, dialect=dialect)

    if sources:
        expression = exp.expand(
            expression,
            {
                k: t.cast(exp.Query, maybe_parse(v, copy=copy, dialect=dialect))
                for k, v in sources.items()
            },
            dialect=dialect,
            copy=copy,
        )

    schema = ensure_schema(schema, dialect=dialect)

    if not scope:
        expression = qualify.qualify(
            expression,
            dialect=dialect,
            schema=schema,
            **{"validate_qualify_columns": False, "identify": False, **kwargs},  # type: ignore
        )
        scope = build_scope(expression)

    if not scope:
        raise SqlglotError("Cannot build lineage, sql must be SELECT")

    selectable = scope.expression
    if not isinstance(selectable, exp.Selectable):
        raise SqlglotError("Cannot build lineage, sql must be a query")

    return scope, schema, selectable


@t.overload
def lineage(
    column: str | exp.Column, sql: str | exp.Expr, schema: dict | Schema, **kwargs: t.Any
) -> Node: ...


@t.overload
def lineage(
    column: None, sql: str | exp.Expr, schema: dict | Schema, **kwargs: t.Any
) -> dict[str, Node]: ...


def lineage(
    column: str | exp.Column | None,
    sql: str | exp.Expr,
    schema: dict | Schema,
    sources: Mapping[str, str | exp.Query] | None = None,
    dialect: DialectType = None,
    scope: Scope | None = None,
    trim_selects: bool = True,
    copy: bool = True,
    on_node: t.Callable[[Node], None] | None = None,
    **kwargs,
) -> Node | dict[str, Node]:
    """Build the lineage graph for a SQL query.

    If `column` is given, returns the lineage Node for that single output column.
    If `column` is None, returns a dict mapping every top-level output column name
    to its lineage Node (with a shared cache so cross-column work is deduplicated).

    Args:
        column: The column to build the lineage for. Pass None to get all output columns.
        sql: The SQL string or expression.
        schema: The schema of tables.
        sources: A mapping of queries which will be used to continue building lineage.
        dialect: The dialect of input SQL.
        scope: A pre-created scope to use instead.
        trim_selects: Whether to clean up selects by trimming to only relevant columns.
        copy: Whether to copy the Expr arguments.
        on_node: Optional callback invoked for every Node created during the walk,
            after the Node's downstream is populated. Useful for injecting
            caller-managed data into Node.payload during the walk.
        **kwargs: Qualification optimizer kwargs.

    Returns:
        A Node when `column` is provided, or a dict[str, Node] when `column` is None.

    Note:
        The name-keyed dict collapses duplicate output aliases and does not
        carry projection positions. Use `lineage_sources` for a position-aware
        report that also surfaces unresolved parts of the graph.
    """
    scope, schema, selectable = _prepare_scope(sql, schema, sources, dialect, scope, copy, kwargs)

    cache: dict[tuple, Node] = {}
    scope_meta: dict[int, tuple[bool, dict[str, exp.Expr]]] = {}

    if column is not None:
        column_name = normalize_identifiers.normalize_identifiers(column, dialect=dialect).name
        if not any(select.alias_or_name == column_name for select in selectable.selects):
            raise SqlglotError(f"Cannot find column '{column_name}' in query.")

        return to_node(
            column_name,
            scope,
            dialect,
            trim_selects=trim_selects,
            schema=schema,
            _cache=cache,
            _scope_meta=scope_meta,
            on_node=on_node,
        )

    result: dict[str, Node] = {}
    for sel in selectable.selects:
        name = sel.alias_or_name
        if not name:
            raise SqlglotError(
                f"Cannot fetch lineage for unnamed projection: {sel.sql(dialect=dialect)}."
            )

        result[name] = to_node(
            name,
            scope,
            dialect,
            trim_selects=trim_selects,
            schema=schema,
            _cache=cache,
            _scope_meta=scope_meta,
            on_node=on_node,
        )

    return result


def lineage_sources(
    column: str | exp.Column | None,
    sql: str | exp.Expr,
    schema: dict | Schema,
    sources: Mapping[str, str | exp.Query] | None = None,
    dialect: DialectType = None,
    scope: Scope | None = None,
    trim_selects: bool = True,
    copy: bool = True,
    on_node: t.Callable[[Node], None] | None = None,
    **kwargs,
) -> list[ColumnLineage]:
    """Build a position-aware source report for a query's output columns.

    This is a companion to `lineage()` aimed at consumers (e.g. export
    services) that need to decide whether an output column derives from a
    sensitive field:

    - Every output projection is reported by `position`, so duplicate output
      aliases (e.g. two projections both named `label`) are not collapsed.
    - `sources` contains the complete set of resolved base sources, including
      every branch of a UNION, even when all branches share one output name.
    - `unresolved` carries the parts that could not be resolved (unexpanded
      `*` without a schema, ambiguous unqualified columns, missing sources),
      each with a reason. An empty `sources` is never a proof that a column is
      public: check `requires_confirmation` and `unresolved` first.

    Args:
        column: Restrict the report to this output column. Pass None to report
            on every top-level output column.
        sql: The SQL string or expression.
        schema: The schema of tables.
        sources: A mapping of queries which will be used to continue building lineage.
        dialect: The dialect of input SQL.
        scope: A pre-created scope to use instead.
        trim_selects: Whether to clean up selects by trimming to only relevant columns.
        copy: Whether to copy the Expr arguments.
        on_node: Optional callback invoked for every Node created during the walk.
        **kwargs: Qualification optimizer kwargs.

    Returns:
        A list of ColumnLineage, one per output projection (per match when
        `column` is provided), in projection order.
    """
    scope, schema, selectable = _prepare_scope(sql, schema, sources, dialect, scope, copy, kwargs)

    selects = selectable.selects
    names = [select.alias_or_name for select in selects]

    if column is not None:
        column_name = normalize_identifiers.normalize_identifiers(column, dialect=dialect).name
        positions = [i for i, name in enumerate(names) if name == column_name]
        if not positions:
            raise SqlglotError(f"Cannot find column '{column_name}' in query.")
    else:
        positions = list(range(len(selects)))

    cache: dict[tuple, Node] = {}
    scope_meta: dict[int, tuple[bool, dict[str, exp.Expr]]] = {}

    report = []
    for position in positions:
        name = names[position]
        # Resolve by position when the name is missing (e.g. `*`) or ambiguous
        # (duplicate output aliases), so each projection gets its own graph.
        key = position if not name or names.count(name) > 1 else name
        node = to_node(
            key,
            scope,
            dialect,
            trim_selects=trim_selects,
            schema=schema,
            _cache=cache,
            _scope_meta=scope_meta,
            on_node=on_node,
        )
        report.append(
            ColumnLineage(
                name=name or selects[position].sql(dialect=dialect),
                position=position,
                node=node,
                sources=node.sources,
                unresolved=node.unresolved,
            )
        )

    return report


def to_node(
    column: str | int,
    scope: Scope,
    dialect: DialectType,
    schema: Schema,
    scope_name: str | None = None,
    upstream: Node | None = None,
    source_name: str | None = None,
    reference_node_name: str | None = None,
    trim_selects: bool = True,
    _cache: dict[tuple, Node] | None = None,
    _scope_meta: dict[int, tuple[bool, dict[str, exp.Expr]]] | None = None,
    on_node: t.Callable[[Node], None] | None = None,
) -> Node:
    cache_key = (column, id(scope), scope_name, source_name, reference_node_name)

    if _cache is not None and cache_key in _cache:
        cached_node = _cache[cache_key]
        if upstream:
            upstream.downstream.append(cached_node)
        return cached_node

    # Find the specific select clause that is the source of the column we want.
    # This can either be a specific, named select or a generic `*` clause.
    selectable = t.cast(exp.Selectable, scope.expression)
    uncertainty_reason = ""
    if isinstance(column, int):
        if column >= len(selectable.selects):
            raise SqlglotError(
                f"Cannot find column's source with index {column} in query: {selectable.sql(dialect=dialect)}"
            )
        select = selectable.selects[column]
    else:
        # Resolving a column to its select scans selectable.selects on every call;
        # memoize a per-scope {name: select} map and is_star bit instead.
        if _scope_meta is None:
            is_star = selectable.is_star
            resolved_select = next(
                (s for s in selectable.selects if s.alias_or_name == column),
                None,
            )
        else:
            scope_id = id(scope)
            meta = _scope_meta.get(scope_id)
            if meta is None:
                select_by_name: dict[str, exp.Expr] = {}
                for sel in selectable.selects:
                    select_by_name.setdefault(sel.alias_or_name, sel)
                meta = (selectable.is_star, select_by_name)
                _scope_meta[scope_id] = meta
            is_star, select_by_name = meta
            resolved_select = select_by_name.get(column)

        if resolved_select is None:
            if is_star:
                select = exp.Star()
                uncertainty_reason = (
                    f"column '{column}' is not explicitly projected and was inferred "
                    f"from an unexpanded '*' projection"
                )
            else:
                select = scope.expression
                uncertainty_reason = (
                    f"column '{column}' could not be found in the projections of this scope"
                )
        else:
            select = resolved_select

    if isinstance(scope.expression, exp.Subquery):
        for inner_scope in scope.subquery_scopes:
            result = to_node(
                column,
                scope=inner_scope,
                dialect=dialect,
                upstream=upstream,
                source_name=source_name,
                reference_node_name=reference_node_name,
                trim_selects=trim_selects,
                schema=schema,
                _cache=_cache,
                _scope_meta=_scope_meta,
                on_node=on_node,
            )
            # Skip caching a passed-in upstream returned by an inner SetOp:
            # a sibling call at the same key with that node as its upstream
            # would otherwise self-loop on the cache hit.
            if _cache is not None and result is not upstream:
                _cache[cache_key] = result
            return result
    if isinstance(scope.expression, exp.SetOperation):
        name = type(scope.expression).__name__.upper()
        created_setop = upstream is None
        upstream = upstream or Node(
            name=name,
            source=scope.expression,
            expression=select,
            uncertainty_reason=uncertainty_reason,
        )

        index = (
            column
            if isinstance(column, int)
            else next(
                (
                    i
                    for i, select in enumerate(selectable.selects)
                    if select.alias_or_name == column or select.is_star
                ),
                -1,  # mypy will not allow a None here, but a negative index should never be returned
            )
        )

        if index == -1:
            raise ValueError(f"Could not find {column} in {scope.expression}")

        for s in scope.set_operation_scopes:
            to_node(
                index,
                scope=s,
                dialect=dialect,
                upstream=upstream,
                source_name=source_name,
                reference_node_name=reference_node_name,
                trim_selects=trim_selects,
                schema=schema,
                _cache=_cache,
                _scope_meta=_scope_meta,
                on_node=on_node,
            )

        if _cache is not None and created_setop:
            _cache[cache_key] = upstream
        if created_setop and on_node:
            on_node(upstream)
        return upstream

    if trim_selects and isinstance(scope.expression, exp.Select):
        # For better ergonomics in our node labels, replace the full select with
        # a version that has only the column we care about.
        #   "x", SELECT x, y FROM foo
        #     => "x", SELECT x FROM foo
        source: exp.Expr = scope.expression.select(select, append=False)
    else:
        source = scope.expression

    subquery_scopes = {
        id(subquery_scope.expression): subquery_scope for subquery_scope in scope.subquery_scopes
    }
    in_scope_subqueries = list(find_all_in_scope(select, *exp.UNWRAPPED_QUERIES))

    reasons = [uncertainty_reason] if uncertainty_reason else []
    for subquery in in_scope_subqueries:
        if id(subquery) not in subquery_scopes:
            logger.warning(f"Unknown subquery scope: {subquery.sql(dialect=dialect)}")
            reasons.append(
                f"subquery scope could not be resolved: {subquery.sql(dialect=dialect)}"
            )

    # Create the node for this step in the lineage chain, and attach it to the previous one.
    node = Node(
        name=f"{scope_name}.{column}" if scope_name else str(column),
        source=source,
        expression=select,
        source_name=source_name or "",
        reference_node_name=reference_node_name or "",
        uncertainty_reason="; ".join(reasons),
    )

    if upstream:
        upstream.downstream.append(node)

    for subquery in in_scope_subqueries:
        subquery_scope: Scope | None = subquery_scopes.get(id(subquery))
        if not subquery_scope:
            continue

        for name in subquery.named_selects:
            to_node(
                name,
                scope=subquery_scope,
                dialect=dialect,
                upstream=node,
                trim_selects=trim_selects,
                schema=schema,
                _cache=_cache,
                _scope_meta=_scope_meta,
                on_node=on_node,
            )

    # if the select is a star add all scope sources as downstreams
    if isinstance(select, exp.Star):
        for src in scope.sources.values():
            src_expr = src.expression if isinstance(src, Scope) else src
            star_node = Node(
                name=select.sql(comments=False),
                source=src_expr,
                expression=src_expr,
                uncertainty_reason=STAR_NOT_EXPANDED_REASON,
            )
            node.downstream.append(star_node)
            if on_node:
                on_node(star_node)

    # Find all columns that went into creating this one to list their lineage nodes.
    source_columns = set(find_all_in_scope(select, exp.Column))

    # If the source is a UDTF find columns used in the UDTF to generate the table
    if isinstance(source, exp.UDTF):
        source_columns |= set(source.find_all(exp.Column))
        derived_tables: Sequence[exp.Expr] = [
            src.expression.parent
            for src in scope.sources.values()
            if isinstance(src, Scope) and src.is_derived_table and src.expression.parent
        ]
    else:
        derived_tables = scope.derived_tables

    source_names = {
        dt.alias: dt.comments[0].split()[1]
        for dt in derived_tables
        if dt.comments and dt.comments[0].startswith("source: ")
    }

    pivots = scope.pivots
    if pivots and pivots[0].parent is not pivots[-1].parent:
        # The scope's pivots only form a chain when they all hang off of the same source;
        # otherwise they can't be folded, so their columns degrade to unresolved leaves
        pivots = []

    pivot_renames: dict[str, str] = {}
    pivot_column_mapping: dict[str, list[exp.Column]] = {}

    if pivots:
        pivot_renames, pivot_column_mapping = _pivot_chain_mapping(pivots, scope, schema)

    for c in source_columns:
        table = c.table
        col_source: exp.Table | Scope | None = scope.sources.get(table)

        # Lineage-specific single-source inference: when the optimizer could not qualify a
        # column (e.g. no schema was provided), attribute it to the sole named source so
        # the graph remains accurate rather than degrading to a Placeholder leaf.
        if col_source is None and not table:
            named_sources = {k: v for k, v in scope.sources.items() if k}
            if len(named_sources) == 1:
                table, col_source = next(iter(named_sources.items()))
                c.set("table", exp.to_identifier(table))

        if isinstance(col_source, Scope):
            reference_node_name = None
            if col_source.scope_type == ScopeType.DERIVED_TABLE and table not in source_names:
                reference_node_name = table
            elif col_source.scope_type == ScopeType.CTE:
                selected_node, _ = scope.selected_sources.get(table, (None, None))
                reference_node_name = selected_node.name if selected_node else None

            # The table itself came from a more specific scope. Recurse into that one using the unaliased column name.
            to_node(
                c.name,
                scope=col_source,
                dialect=dialect,
                scope_name=table,
                upstream=node,
                source_name=source_names.get(table) or source_name,
                reference_node_name=reference_node_name,
                trim_selects=trim_selects,
                schema=schema,
                _cache=_cache,
                _scope_meta=_scope_meta,
                on_node=on_node,
            )
        elif pivots and pivots[-1].alias_or_name == c.table:
            # Only the last operator in a chain names the resulting source
            pivot_parent = pivots[-1].parent
            downstream_columns = []

            column_name = c.name
            if column_name in pivot_column_mapping:
                downstream_columns.extend(pivot_column_mapping[column_name])
            else:
                # The column is not in the pivot, so it must be an implicit column of the
                # pivoted source -- adapt column to be from the implicit pivoted source.
                downstream_columns.append(
                    exp.column(
                        pivot_renames.get(c.name, c.this),
                        table=pivot_parent.alias_or_name if pivot_parent else None,
                    )
                )

            for downstream_column in downstream_columns:
                if not downstream_column.table:
                    # Some dialects (e.g. bigquery) don't qualify the IN-list columns,
                    # but they can only come from the pivoted source
                    downstream_column = exp.column(
                        downstream_column.this,
                        table=pivot_parent.alias_or_name if pivot_parent else None,
                    )

                table = downstream_column.table
                col_source = scope.sources.get(table)
                if isinstance(col_source, exp.Table) and not col_source.db:
                    # A pivoted CTE reference maps to the raw table in `scope.sources`,
                    # so recover the CTE's scope to keep tracing through it
                    col_source = scope.cte_sources.get(col_source.name, col_source)
                if isinstance(col_source, Scope):
                    to_node(
                        downstream_column.name,
                        scope=col_source,
                        scope_name=table,
                        dialect=dialect,
                        upstream=node,
                        source_name=source_names.get(table) or source_name,
                        reference_node_name=reference_node_name,
                        trim_selects=trim_selects,
                        schema=schema,
                        _cache=_cache,
                        _scope_meta=_scope_meta,
                        on_node=on_node,
                    )
                else:
                    col_expr = col_source or exp.Placeholder()
                    pivot_leaf = Node(
                        name=downstream_column.sql(comments=False),
                        source=col_expr,
                        expression=col_expr,
                        uncertainty_reason=(
                            ""
                            if col_source is not None
                            else _unresolved_column_reason(downstream_column, scope)
                        ),
                    )
                    node.downstream.append(pivot_leaf)
                    if on_node:
                        on_node(pivot_leaf)
        else:
            # The source is not a scope and the column is not in any pivot - we've reached the end
            # of the line. At this point, if a source is not found it means this column's lineage
            # is unknown. This can happen if the definition of a source used in a query is not
            # passed into the `sources` map.
            col_expr = col_source or exp.Placeholder()
            leaf = Node(
                name=c.sql(comments=False),
                source=col_expr,
                expression=col_expr,
                uncertainty_reason=_leaf_uncertainty_reason(c, col_source, scope),
            )
            node.downstream.append(leaf)
            if on_node:
                on_node(leaf)

    if _cache is not None:
        _cache[cache_key] = node

    if on_node:
        on_node(node)

    return node


def _unresolved_column_reason(column: exp.Column, scope: Scope) -> str:
    """Explain why a column leaf could not be attributed to a source."""
    table = column.table
    if table:
        return (
            f"column '{column.sql(comments=False)}' could not be resolved: "
            f"source '{table}' is not available in this scope"
        )

    named_sources = sorted(name for name in scope.sources if name)
    if len(named_sources) > 1:
        return (
            f"unqualified column '{column.name}' is ambiguous: it cannot be attributed "
            f"to a single source (candidates: {', '.join(named_sources)})"
        )
    if not named_sources:
        return (
            f"unqualified column '{column.name}' could not be resolved: "
            f"the scope has no sources"
        )
    return f"column '{column.name}' could not be resolved"


def _leaf_uncertainty_reason(
    column: exp.Column, col_source: exp.Table | Scope | None, scope: Scope
) -> str:
    if col_source is None:
        return _unresolved_column_reason(column, scope)
    if isinstance(column.this, exp.Star):
        return STAR_NOT_EXPANDED_REASON
    return ""


def _source_ref(node: Node) -> SourceRef | None:
    source = node.source
    if isinstance(source, exp.Table):
        return SourceRef(
            table=exp.table_name(source),
            column=node.name.rsplit(".", 1)[-1],
            expression=source.sql(comments=False),
        )
    if isinstance(source, (exp.Values, exp.UDTF)):
        return SourceRef(table="", column=node.name, expression=source.sql(comments=False))
    return None


def _collect_refs(node: Node) -> tuple[tuple[SourceRef, ...], tuple[UnresolvedSource, ...]]:
    """Collect resolved base sources and unresolved parts under a node.

    Unresolved subtrees are pruned: anything below them is suspect, so it is
    reported as unresolved rather than being mistaken for a resolved source.
    """
    sources: list[SourceRef] = []
    unresolved: list[UnresolvedSource] = []
    visited: set[int] = set()

    def visit(current: Node) -> None:
        if id(current) in visited:
            return
        visited.add(id(current))

        if current.is_unresolved:
            unresolved.append(
                UnresolvedSource(
                    reason=current.uncertainty_reason or "source could not be resolved",
                    name=current.name,
                    expression=current.source.sql(comments=False),
                )
            )
            return

        if not current.downstream:
            ref = _source_ref(current)
            if ref is not None:
                sources.append(ref)
            return

        for child in current.downstream:
            visit(child)

    visit(node)
    return tuple(dict.fromkeys(sources)), tuple(dict.fromkeys(unresolved))


def _pre_pivot_columns(pivot: exp.Pivot, scope: Scope, schema: Schema) -> list[str]:
    """
    The columns the first operator of a chain sees, taken from the projections of a
    derived table or CTE source, or from the schema for a physical table. Returns an
    empty list when they can't be determined (e.g. an unexpanded star), since anything
    positional over them would silently shift.
    """
    parent = pivot.parent
    columns: list[str] = []
    if isinstance(parent, exp.DerivedTable) and isinstance(parent.this, exp.Query):
        columns = parent.this.named_selects
    elif isinstance(parent, exp.Table):
        cte_source = scope.cte_sources.get(parent.name) if not parent.db else None
        if isinstance(cte_source, Scope) and isinstance(cte_source.expression, exp.Query):
            columns = cte_source.expression.named_selects
        elif schema is not None:
            columns = list(schema.column_names(parent, only_visible=True))

    return [] if "*" in columns else columns


def _pivot_chain_mapping(
    pivots: list[exp.Pivot], scope: Scope, schema: Schema
) -> tuple[dict[str, str], dict[str, list[exp.Column]]]:
    """
    Fold a chain of (UN)PIVOT operators into a single view of its output, since each one
    consumes the previous one's columns rather than the pivoted source's.

    Returns the composed output-name -> pre-chain-name renames (from alias column lists),
    and the composed output-name -> source columns it derives from.
    """
    available = _pre_pivot_columns(pivots[0], scope, schema)
    renames: dict[str, str] = {}
    mapping: dict[str, list[exp.Column]] = {}

    for pivot in pivots:
        # Renames are positional over the operator's full output, so they can only be
        # applied when the columns going into it are known
        step_renames = (
            pivot.output_columns(available) if pivot.alias_column_names and available else {}
        )
        step_mapping = _pivot_column_mapping(pivot)
        if step_renames:
            step_mapping = {
                post: step_mapping[pre] for post, pre in step_renames.items() if pre in step_mapping
            }

        # Columns this operator consumed may have been produced by an earlier one, or be
        # alias-list renames of passthroughs; resolve through what we've folded so far
        def resolve(col: exp.Column) -> list[exp.Column]:
            if col.name in mapping:
                return mapping[col.name]
            if col.name in renames:
                return [exp.column(renames[col.name])]
            return [col]

        composed = {
            out: [resolved for col in cols for resolved in resolve(col)]
            for out, cols in step_mapping.items()
        }

        # Whatever an earlier operator produced and this one didn't consume passes through,
        # under whatever name this operator's alias column list gives it
        consumed = {col.name for cols in step_mapping.values() for col in cols}
        pre_to_post = {pre: post for post, pre in step_renames.items()}
        for out, cols in mapping.items():
            if out not in consumed:
                composed.setdefault(pre_to_post.get(out, out), cols)

        renames = (
            {post: renames.get(pre, pre) for post, pre in step_renames.items()}
            if step_renames
            else renames
        )
        mapping = composed
        available = list(pivot.output_columns(available)) if available else []

    return renames, mapping


def _pivot_column_mapping(pivot: exp.Pivot) -> dict[str, list[exp.Column]]:
    """Map each (UN)PIVOT output column name to the source columns it's derived from."""
    mapping: dict[str, list[exp.Column]] = {}

    if pivot.unpivot:
        # UNPIVOT((v1, v2) FOR name IN ((a1, a2), (b1, b2))): each value column is derived
        # positionally from the IN-list entries, and the name column from all of them
        value_columns = [
            identifier for e in pivot.expressions for identifier in e.find_all(exp.Identifier)
        ]
        for value_column in value_columns:
            mapping[value_column.name] = []

        for field in pivot.fields:
            if not isinstance(field, exp.In):
                continue

            name_columns = mapping.setdefault(field.this.name, [])
            for entry in field.expressions:
                entry_columns = list(entry.find_all(exp.Column))
                name_columns.extend(entry_columns)

                if len(entry_columns) == len(value_columns):
                    for value_column, column in zip(value_columns, entry_columns):
                        mapping[value_column.name].append(column)
                else:
                    for value_column in value_columns:
                        mapping[value_column.name].extend(entry_columns)

        return mapping

    # For each aggregation function, the pivot creates a new column for each field in category
    # combined with the aggfunc. So the columns parsed have this order: cat_a_value_sum, cat_a,
    # b_value_sum, b. Because of this step wise manner the aggfunc 'sum(value) as value_sum'
    # belongs to the column indices 0, 2, and the aggfunc 'max(price)' without an alias belongs
    # to the column indices 1, 3. Here, only the columns used in the aggregations are of interest
    # in the lineage, so lookup the pivot column name by index and map that with the columns used
    # in the aggregation.
    #
    # Example: PIVOT (SUM(value) AS value_sum, MAX(price)) FOR category IN ('a' AS cat_a, 'b')
    pivot_columns = pivot.args["columns"]
    pivot_aggs_count = len(pivot.expressions)

    mapping = {}
    for i, agg in enumerate(pivot.expressions):
        agg_cols = list(agg.find_all(exp.Column))
        for col_index in range(i, len(pivot_columns), pivot_aggs_count):
            mapping[pivot_columns[col_index].name] = agg_cols
    return mapping


class GraphHTML:
    """Node to HTML generator using vis.js.

    https://visjs.github.io/vis-network/docs/network/
    """

    def __init__(
        self,
        nodes: dict,
        edges: list,
        imports: bool = True,
        options: Mapping[str, object] | None = None,
    ):
        self.imports = imports

        self.options = {
            "height": "500px",
            "width": "100%",
            "layout": {
                "hierarchical": {
                    "enabled": True,
                    "nodeSpacing": 200,
                    "sortMethod": "directed",
                },
            },
            "interaction": {
                "dragNodes": False,
                "selectable": False,
            },
            "physics": {
                "enabled": False,
            },
            "edges": {
                "arrows": "to",
            },
            "nodes": {
                "font": "20px monaco",
                "shape": "box",
                "widthConstraint": {
                    "maximum": 300,
                },
            },
            **(options or {}),
        }

        self.nodes = nodes
        self.edges = edges

    def __str__(self):
        nodes = json.dumps(list(self.nodes.values()))
        edges = json.dumps(self.edges)
        options = json.dumps(self.options)
        imports = (
            """<script type="text/javascript" src="https://unpkg.com/vis-data@latest/peer/umd/vis-data.min.js"></script>
  <script type="text/javascript" src="https://unpkg.com/vis-network@latest/peer/umd/vis-network.min.js"></script>
  <link rel="stylesheet" type="text/css" href="https://unpkg.com/vis-network/styles/vis-network.min.css" />"""
            if self.imports
            else ""
        )

        return f"""<div>
  <div id="sqlglot-lineage"></div>
  {imports}
  <script type="text/javascript">
    var nodes = new vis.DataSet({nodes})
    nodes.forEach(row => row["title"] = new DOMParser().parseFromString(row["title"], "text/html").body.childNodes[0])

    new vis.Network(
        document.getElementById("sqlglot-lineage"),
        {{
            nodes: nodes,
            edges: new vis.DataSet({edges})
        }},
        {options},
    )
  </script>
</div>"""

    def _repr_html_(self) -> str:
        return self.__str__()
