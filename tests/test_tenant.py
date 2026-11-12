import sqlite3
import unittest

import sqlglot
from sqlglot import exp
from sqlglot.tenant import TenantFilterError, add_tenant_filter


class TestTenantFilter(unittest.TestCase):
    def filter(self, sql, **kwargs):
        kwargs.setdefault("tables", {"orders": "tenant_id", "customers": "tenant_id"})
        kwargs.setdefault("tenant", "acme")
        return add_tenant_filter(sql, **kwargs).sql()

    def test_simple_select(self):
        self.assertEqual(
            self.filter("SELECT * FROM orders"),
            "SELECT * FROM (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS orders",
        )

    def test_alias(self):
        self.assertEqual(
            self.filter("SELECT o.id FROM orders AS o"),
            "SELECT o.id FROM (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS o",
        )

    def test_alias_with_column_list(self):
        self.assertEqual(
            self.filter("SELECT * FROM orders AS o(a, b)"),
            "SELECT * FROM (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS o(a, b)",
        )

    def test_qualified_table(self):
        self.assertEqual(
            self.filter("SELECT * FROM main.orders"),
            "SELECT * FROM (SELECT * FROM main.orders WHERE orders.tenant_id = 'acme') AS orders",
        )

    def test_qualified_key_must_match(self):
        self.assertEqual(
            self.filter("SELECT * FROM main.orders", tables={"other.orders": "tenant_id"}),
            "SELECT * FROM main.orders",
        )

    def test_unprotected_table_untouched(self):
        self.assertEqual(
            self.filter("SELECT * FROM orders JOIN regions ON regions.id = orders.region_id"),
            "SELECT * FROM (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS orders "
            "JOIN regions ON regions.id = orders.region_id",
        )

    def test_repeated_table_reference(self):
        self.assertEqual(
            self.filter("SELECT * FROM orders a JOIN orders b ON a.parent_id = b.id"),
            "SELECT * FROM (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS a "
            "JOIN (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS b "
            "ON a.parent_id = b.id",
        )

    def test_left_join_preserves_null_side(self):
        self.assertEqual(
            self.filter(
                "SELECT c.name FROM customers c LEFT JOIN orders o ON o.customer_id = c.id"
            ),
            "SELECT c.name FROM "
            "(SELECT * FROM customers WHERE customers.tenant_id = 'acme') AS c "
            "LEFT JOIN (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS o "
            "ON o.customer_id = c.id",
        )

    def test_subquery(self):
        self.assertEqual(
            self.filter(
                "SELECT * FROM orders WHERE id IN (SELECT order_id FROM items)",
                tables={"orders": "tenant_id", "items": "tenant_id"},
            ),
            "SELECT * FROM (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS orders "
            "WHERE id IN (SELECT order_id FROM (SELECT * FROM items WHERE items.tenant_id = 'acme') AS items)",
        )

    def test_cte_shadowing_not_filtered(self):
        self.assertEqual(
            self.filter("WITH orders AS (SELECT 1 AS id) SELECT * FROM orders"),
            "WITH orders AS (SELECT 1 AS id) SELECT * FROM orders",
        )
        self.assertEqual(
            self.filter("WITH orders AS (SELECT 1 AS id) SELECT * FROM orders AS o"),
            "WITH orders AS (SELECT 1 AS id) SELECT * FROM orders AS o",
        )

    def test_cte_body_filtered(self):
        self.assertEqual(
            self.filter("WITH f AS (SELECT * FROM orders) SELECT * FROM f"),
            "WITH f AS (SELECT * FROM (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS orders) "
            "SELECT * FROM f",
        )

    def test_set_operation(self):
        self.assertEqual(
            self.filter("SELECT id FROM orders UNION ALL SELECT id FROM orders"),
            "SELECT id FROM (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS orders "
            "UNION ALL SELECT id FROM (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS orders",
        )

    def test_tenant_value_types(self):
        self.assertEqual(
            self.filter("SELECT * FROM orders", tenant=42),
            "SELECT * FROM (SELECT * FROM orders WHERE orders.tenant_id = 42) AS orders",
        )
        self.assertEqual(
            self.filter("SELECT * FROM orders", tenant=exp.Placeholder(this="tid")),
            "SELECT * FROM (SELECT * FROM orders WHERE orders.tenant_id = :tid) AS orders",
        )

    def test_shared_column_for_table_collection(self):
        self.assertEqual(
            self.filter(
                "SELECT * FROM orders JOIN customers ON customers.id = orders.customer_id",
                tables=["orders", "customers"],
                column="org_id",
            ),
            "SELECT * FROM (SELECT * FROM orders WHERE orders.org_id = 'acme') AS orders "
            "JOIN (SELECT * FROM customers WHERE customers.org_id = 'acme') AS customers "
            "ON customers.id = orders.customer_id",
        )

    def test_expression_input(self):
        expression = sqlglot.parse_one("SELECT * FROM orders")
        rewritten = add_tenant_filter(expression, tables=["orders"], tenant="acme")
        self.assertEqual(
            rewritten.sql(),
            "SELECT * FROM (SELECT * FROM orders WHERE orders.tenant_id = 'acme') AS orders",
        )
        self.assertEqual(expression.sql(), "SELECT * FROM orders")

    def test_rejects_non_select(self):
        for sql in (
            "DELETE FROM orders",
            "UPDATE orders SET tenant_id = 'acme'",
            "CREATE TABLE t (id INT)",
        ):
            with self.assertRaises(TenantFilterError, msg=sql):
                self.filter(sql)

    def test_rejects_unsupported_table_features(self):
        with self.assertRaises(TenantFilterError):
            self.filter("SELECT * FROM orders TABLESAMPLE (10)")

    def test_rejects_ambiguous_duplicate_source(self):
        with self.assertRaises(TenantFilterError):
            self.filter("SELECT * FROM orders, orders")


class TestTenantFilterSQLite(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE customers (id INTEGER, tenant_id TEXT, name TEXT);
            CREATE TABLE orders (id INTEGER, tenant_id TEXT, customer_id INTEGER, amount INTEGER);
            """
        )
        self.db.executemany(
            "INSERT INTO customers VALUES (?, ?, ?)",
            [
                (1, "acme", "Alice"),
                (2, "acme", "Bob"),
                (3, "other", "Mallory"),
            ],
        )
        self.db.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?)",
            [
                (10, "acme", 1, 100),
                # another tenant's order pointing at an acme customer: must never leak
                (11, "other", 1, 999),
                (12, "other", 3, 50),
            ],
        )

    def tearDown(self):
        self.db.close()

    def run_query(self, sql, tenant="acme"):
        rewritten = add_tenant_filter(
            sql, tables=["customers", "orders"], tenant=tenant, dialect="sqlite"
        )
        return self.db.execute(rewritten.sql("sqlite")).fetchall()

    def test_left_join_keeps_unmatched_left_rows(self):
        rows = self.run_query(
            "SELECT c.name, o.amount FROM customers c "
            "LEFT JOIN orders o ON o.customer_id = c.id ORDER BY c.id"
        )
        # Bob has no orders but is still visible; order 11 belongs to another tenant and
        # must not leak through the right side of the join
        self.assertEqual(rows, [("Alice", 100), ("Bob", None)])

    def test_inner_join(self):
        rows = self.run_query(
            "SELECT c.name, o.amount FROM customers c "
            "JOIN orders o ON o.customer_id = c.id ORDER BY o.id"
        )
        self.assertEqual(rows, [("Alice", 100)])

    def test_subquery(self):
        rows = self.run_query(
            "SELECT c.name FROM customers c WHERE c.id IN (SELECT customer_id FROM orders)"
        )
        self.assertEqual(rows, [("Alice",)])

    def test_other_tenant(self):
        rows = self.run_query(
            "SELECT c.name, o.amount FROM customers c "
            "LEFT JOIN orders o ON o.customer_id = c.id ORDER BY c.id",
            tenant="other",
        )
        self.assertEqual(rows, [("Mallory", 50)])

    def test_cte_shadowing(self):
        rows = self.run_query("WITH orders AS (SELECT 50 AS amount) SELECT * FROM orders")
        self.assertEqual(rows, [(50,)])


if __name__ == "__main__":
    unittest.main()
