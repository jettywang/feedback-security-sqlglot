"""SQLite differential checks for subquery unnesting around NULL-sensitive predicates.

The optimizer rewrites predicates such as ``IN`` / ``EXISTS`` into anti/semi joins.
A LEFT-JOIN anti rewrite of ``NOT IN`` cannot express SQL's three-valued logic when
the blacklist column contains NULL: ``x NOT IN (..., NULL, ...)`` evaluates to
UNKNOWN for every row, while a join-based rewrite would keep unmatched rows. The
optimizer therefore has to keep such subqueries untouched.

These tests use only the stdlib ``sqlite3`` engine. For every case they execute
both the original query and the query produced by ``optimizer.optimize`` and compare
the result with the allowed rows written explicitly below.
"""

import sqlite3
import unittest

from sqlglot import exp, optimizer

X_ROWS = [
    (1, 1),
    (2, 2),
    (3, 3),
    (None, 4),
    (5, None),
]

# Each dataset is a different shape of the blacklist table y(b, c).
DATASETS = {
    "empty": [],
    "non_null": [(1, 1), (2, 2), (4, 4)],
    "with_null": [(1, 1), (None, 2), (4, 4)],
    "duplicates": [(1, 1), (1, 1), (2, 2), (2, 3), (None, 5), (None, 5)],
    "only_null": [(None, 1)],
}

SCHEMA = {"x": {"a": "INT", "b": "INT"}, "y": {"b": "INT", "c": "INT"}}

# Allowed rows per (dataset, case), derived once from SQL's three-valued logic and
# pinned here so the test fails if the optimizer silently changes the semantics.
# Single-column cases pin the first column; "notin_proj" pins full (a, r) pairs.
EXPECTED = {
    "empty": {
        "notin": [None, 1, 2, 3, 5],
        "notin_corr": [None, 1, 2, 3, 5],
        "notin_corr_ne": [None, 1, 2, 3, 5],
        "notexists": [None, 1, 2, 3, 5],
        "notexists_2p": [None, 1, 2, 3, 5],
        "notexists_uncorr": [None, 1, 2, 3, 5],
        "notin_proj": [(None, 1), (1, 1), (2, 1), (3, 1), (5, 1)],
        "notin_or": [None, 1, 2, 3, 5],
        "exists_pos": [],
    },
    "non_null": {
        "notin": [3, 5],
        "notin_corr": [3, 5],
        "notin_corr_ne": [1, 2, 3, 5],
        "notexists": [None, 3, 5],
        "notexists_2p": [None, 3, 5],
        "notexists_uncorr": [],
        "notin_proj": [(None, None), (1, 0), (2, 0), (3, 1), (5, 1)],
        "notin_or": [3, 5],
        "exists_pos": [1, 2],
    },
    "with_null": {
        # NULL anywhere in the blacklist makes NOT IN UNKNOWN for every row.
        "notin": [],
        "notin_corr": [3, 5],
        "notin_corr_ne": [2, 5],
        "notexists": [None, 2, 3, 5],
        "notexists_2p": [None, 2, 3, 5],
        "notexists_uncorr": [],
        "notin_proj": [(None, None), (1, 0), (2, None), (3, None), (5, None)],
        "notin_or": [5],
        "exists_pos": [1],
    },
    "duplicates": {
        "notin": [],
        "notin_corr": [None, 3, 5],
        "notin_corr_ne": [5],
        "notexists": [None, 3, 5],
        "notexists_2p": [None, 3, 5],
        "notexists_uncorr": [],
        "notin_proj": [(None, None), (1, 0), (2, 0), (3, None), (5, None)],
        "notin_or": [5],
        "exists_pos": [1, 2],
    },
    "only_null": {
        "notin": [],
        "notin_corr": [None, 2, 3, 5],
        "notin_corr_ne": [1, 5],
        "notexists": [None, 1, 2, 3, 5],
        "notexists_2p": [None, 1, 2, 3, 5],
        "notexists_uncorr": [],
        "notin_proj": [(None, None), (1, None), (2, None), (3, None), (5, None)],
        "notin_or": [5],
        "exists_pos": [],
    },
}

QUERIES = {
    # uncorrelated NOT IN: empty list allows everything, any NULL denies everything
    "notin": "SELECT x.a FROM x WHERE x.a NOT IN (SELECT y.b FROM y) ORDER BY x.a",
    # correlated NOT IN keeps its subquery per correlation group
    "notin_corr": (
        "SELECT x.a FROM x WHERE x.a NOT IN " "(SELECT y.b FROM y WHERE y.c = x.b) ORDER BY x.a"
    ),
    "notin_corr_ne": (
        "SELECT x.a FROM x WHERE x.a NOT IN " "(SELECT y.b FROM y WHERE y.c <> x.b) ORDER BY x.a"
    ),
    # correlated NOT EXISTS: NULL never matches `=`, so NULL blacklist rows are invisible
    "notexists": (
        "SELECT x.a FROM x WHERE NOT EXISTS " "(SELECT 1 FROM y WHERE y.b = x.a) ORDER BY x.a"
    ),
    "notexists_2p": (
        "SELECT x.a FROM x WHERE NOT EXISTS "
        "(SELECT 1 FROM y WHERE y.b = x.a AND y.c > 0) ORDER BY x.a"
    ),
    "notexists_uncorr": ("SELECT x.a FROM x WHERE NOT EXISTS (SELECT 1 FROM y) ORDER BY x.a"),
    # NOT IN as a projected boolean must keep its UNKNOWN results, not coerce them
    "notin_proj": ("SELECT x.a, x.a NOT IN (SELECT y.b FROM y) AS r FROM x ORDER BY x.a"),
    # NOT IN inside an OR still has to follow three-valued logic
    "notin_or": (
        "SELECT x.a FROM x WHERE x.a NOT IN (SELECT y.b FROM y) " "OR x.b IS NULL ORDER BY x.a"
    ),
    # positive control: plain EXISTS is safe to unnest and must still match
    "exists_pos": (
        "SELECT x.a FROM x WHERE EXISTS " "(SELECT 1 FROM y WHERE y.b = x.a) ORDER BY x.a"
    ),
}

# Shapes that cannot be expressed with a NULL-aware anti join: the optimizer must
# keep the original subquery predicate rather than emit an unsafe join rewrite.
STRUCTURE_PRESERVED = [
    (exp.In, "SELECT x.a FROM x WHERE x.a NOT IN (SELECT y.b FROM y)"),
    (exp.In, "SELECT x.a FROM x WHERE NOT (x.a IN (SELECT y.b FROM y))"),
    (
        exp.In,
        "SELECT x.a FROM x WHERE x.a NOT IN " "(SELECT y.b FROM y WHERE y.c = x.b)",
    ),
    (
        exp.In,
        "SELECT x.a FROM x WHERE (x.a NOT IN (SELECT y.b FROM y)) AND x.b > 0",
    ),
    (
        exp.In,
        "SELECT x.a FROM x WHERE (x.a NOT IN (SELECT y.b FROM y)) OR x.b > 0",
    ),
    (exp.In, "SELECT x.a, x.a NOT IN (SELECT y.b FROM y) AS r FROM x"),
    (
        exp.In,
        "SELECT x.a FROM x GROUP BY x.a HAVING x.a NOT IN (SELECT y.b FROM y)",
    ),
    (
        exp.In,
        "SELECT x.a FROM x WHERE x.a NOT IN " "(SELECT y.b FROM y UNION ALL SELECT y.b FROM y)",
    ),
    (
        exp.Exists,
        "SELECT x.a FROM x WHERE NOT EXISTS " "(SELECT 1 FROM y WHERE NOT (y.b = x.a))",
    ),
]


def _connection(dataset):
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE x (a INT, b INT)")
    connection.execute("CREATE TABLE y (b INT, c INT)")
    connection.executemany("INSERT INTO x VALUES (?, ?)", X_ROWS)
    connection.executemany("INSERT INTO y VALUES (?, ?)", DATASETS[dataset])
    return connection


def _normalize(case, rows):
    # The projection case compares full (a, boolean) tuples; every other case is
    # a single-column allowed list.
    if case == "notin_proj":
        return rows
    return [row[0] for row in rows]


class TestNotInUnnestSQLite(unittest.TestCase):
    def _optimize(self, sql):
        return optimizer.optimize(sql, schema=SCHEMA, dialect="sqlite").sql("sqlite")

    def test_expected_rows_match_pinned_three_valued_logic(self):
        # First validate the pinned expectations against the unoptimized query.
        for dataset, cases in EXPECTED.items():
            connection = _connection(dataset)
            for case, expected in cases.items():
                with self.subTest(dataset=dataset, case=case):
                    rows = connection.execute(QUERIES[case]).fetchall()
                    self.assertEqual(_normalize(case, rows), expected)

    def test_optimized_query_matches_original_on_sqlite(self):
        for dataset, cases in EXPECTED.items():
            for case, expected in cases.items():
                sql = QUERIES[case]
                optimized = self._optimize(sql)
                with self.subTest(dataset=dataset, case=case):
                    connection = _connection(dataset)
                    rows = connection.execute(optimized).fetchall()
                    self.assertEqual(
                        _normalize(case, rows),
                        expected,
                        msg=f"optimized query diverged:\n{optimized}",
                    )

    def test_unsemantic_shapes_keep_their_subquery_structure(self):
        for predicate_type, sql in STRUCTURE_PRESERVED:
            with self.subTest(sql=sql):
                optimized = optimizer.optimize(sql, schema=SCHEMA, dialect="sqlite")
                self.assertIsNotNone(
                    optimized.find(predicate_type),
                    msg=f"subquery was unnested unsafely:\n{optimized.sql('sqlite')}",
                )

    def test_null_blacklist_denies_every_not_in_row(self):
        # The core three-valued-logic guarantee: a single NULL in the blacklist
        # turns every NOT IN result into UNKNOWN and the WHERE clause keeps no row.
        for dataset in ("with_null", "duplicates", "only_null"):
            connection = _connection(dataset)
            original = connection.execute(QUERIES["notin"]).fetchall()
            optimized_rows = connection.execute(self._optimize(QUERIES["notin"])).fetchall()
            with self.subTest(dataset=dataset):
                self.assertEqual(original, [])
                self.assertEqual(optimized_rows, [])

    def test_empty_blacklist_allows_every_not_in_row(self):
        connection = _connection("empty")
        # SQLite sorts NULL first, which is the ordering of the ORDER BY in the query.
        expected = [None, 1, 2, 3, 5]
        original = [row[0] for row in connection.execute(QUERIES["notin"]).fetchall()]
        optimized = [
            row[0] for row in connection.execute(self._optimize(QUERIES["notin"])).fetchall()
        ]
        self.assertEqual(original, expected)
        self.assertEqual(optimized, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
