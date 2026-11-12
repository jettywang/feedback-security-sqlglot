"""Runnable before/after comparison of sqlglot unnesting against SQLite.

Usage:
    .venv/bin/python scripts/not_in_unnest_sqlite_check.py

For every blacklist shape (empty / non-NULL / NULL / duplicates / NULL-only) and
every predicate shape (uncorrelated NOT IN, correlated NOT IN, NOT EXISTS, ...),
the script executes the original query and the query emitted by
``optimizer.optimize`` on the same in-memory SQLite database and prints whether
the two agree. It also shows which shapes keep their subquery structure because a
join rewrite would break three-valued NULL logic.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlglot import optimizer

from tests.test_optimizer_not_in_sqlite import (
    DATASETS,
    EXPECTED,
    QUERIES,
    SCHEMA,
    STRUCTURE_PRESERVED,
    X_ROWS,
    _normalize,
)


def connection(dataset):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE x (a INT, b INT)")
    conn.execute("CREATE TABLE y (b INT, c INT)")
    conn.executemany("INSERT INTO x VALUES (?, ?)", X_ROWS)
    conn.executemany("INSERT INTO y VALUES (?, ?)", DATASETS[dataset])
    return conn


def main():
    failures = 0

    print("=== result parity: original query vs optimizer output on SQLite ===")
    for dataset, cases in EXPECTED.items():
        print(f"\nblacklist '{dataset}' -> y rows: {DATASETS[dataset] or '(empty)'}")
        for case, pinned in cases.items():
            sql = QUERIES[case]
            optimized = optimizer.optimize(sql, schema=SCHEMA, dialect="sqlite").sql("sqlite")
            conn = connection(dataset)
            original = _normalize(case, conn.execute(sql).fetchall())
            actual = _normalize(case, conn.execute(optimized).fetchall())
            ok = original == actual == pinned
            failures += not ok
            print(f"  [{'OK ' if ok else 'FAIL'}] {case:17} allowed={actual}")
            if not ok:
                print(f"        original={original} pinned={pinned}")
                print(f"        optimized SQL: {optimized}")

    print("\n=== shapes that must retain the subquery structure ===")
    for predicate_type, sql in STRUCTURE_PRESERVED:
        optimized = optimizer.optimize(sql, schema=SCHEMA, dialect="sqlite")
        kept = optimized.find(predicate_type) is not None
        failures += not kept
        print(f"  [{'KEPT' if kept else 'LOST'}] {predicate_type.__name__:6} | {sql}")

    print("\n" + ("all comparisons agree" if not failures else f"{failures} failures"))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
