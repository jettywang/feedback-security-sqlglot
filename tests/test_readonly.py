import unittest

from sqlglot.readonly import classify_readonly


class TestReadOnly(unittest.TestCase):
    def assert_readonly(self, sql, dialect="postgres"):
        report = classify_readonly(sql, dialect=dialect)
        self.assertTrue(report.readonly, msg=f"{sql}: {report.violations}")
        self.assertEqual(report.violations, [])

    def assert_rejected(self, sql, dialect="postgres", statement=1, reason_part=""):
        report = classify_readonly(sql, dialect=dialect)
        self.assertFalse(report.readonly, msg=sql)
        self.assertTrue(report.violations)
        matching = [v for v in report.violations if v.statement == statement]
        self.assertTrue(matching, msg=f"{sql}: {report.violations}")
        if reason_part:
            self.assertTrue(
                any(reason_part in v.reason for v in matching),
                msg=f"{sql}: {report.violations}",
            )
        return report

    def test_plain_select(self):
        self.assert_readonly("SELECT a, b FROM t WHERE a > 1")

    def test_union_and_nested_select(self):
        self.assert_readonly("SELECT a FROM t UNION ALL SELECT a FROM u")
        self.assert_readonly("SELECT * FROM (SELECT a FROM t) s WHERE s.a IN (SELECT a FROM u)")

    def test_readonly_cte(self):
        self.assert_readonly("WITH x AS (SELECT a FROM t), y AS (SELECT * FROM x) SELECT * FROM y")

    def test_values(self):
        self.assert_readonly("VALUES (1, 2), (3, 4)")

    def test_write_keywords_in_comments_and_strings(self):
        self.assert_readonly("SELECT 'DELETE FROM t' AS s, 'drop' AS d FROM t")
        self.assert_readonly("SELECT a FROM t -- UPDATE t SET a = 1\nWHERE a <> 'MERGE'")
        self.assert_readonly("/* INSERT INTO t VALUES (1) */ SELECT a FROM t")

    def test_trailing_semicolon(self):
        self.assert_readonly("SELECT 1;")

    def test_modifying_cte(self):
        report = self.assert_rejected(
            "WITH t AS (UPDATE x SET a = 1 RETURNING *) SELECT * FROM t",
            reason_part="data-modifying CTE",
        )
        self.assertIn("UPDATE", report.violations[0].sql)

        self.assert_rejected(
            "WITH d AS (DELETE FROM x RETURNING *) SELECT * FROM d",
            reason_part="Delete",
        )

    def test_select_into(self):
        report = self.assert_rejected(
            "SELECT a INTO new_table FROM t", dialect="tsql", reason_part="INTO"
        )
        self.assertIn("new_table", report.violations[0].sql)

    def test_multiple_statements(self):
        report = self.assert_rejected("SELECT 1; SELECT 2", statement=2, reason_part="2")
        self.assertEqual(report.violations[0].sql, "SELECT 2")
        self.assert_rejected("SELECT 1; DROP TABLE t", statement=2)

    def test_command(self):
        report = self.assert_rejected("VACUUM my_table", reason_part="Command")
        self.assertIn("VACUUM", report.violations[0].sql)

    def test_dml_and_ddl_statements(self):
        self.assert_rejected("INSERT INTO t SELECT * FROM s", reason_part="Insert")
        self.assert_rejected("UPDATE t SET a = 1", reason_part="Update")
        self.assert_rejected("DELETE FROM t", reason_part="Delete")
        self.assert_rejected("MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN DELETE")
        self.assert_rejected("CREATE TABLE t (a INT)", reason_part="Create")
        self.assert_rejected("DROP TABLE t", reason_part="Drop")

    def test_unparseable(self):
        report = self.assert_rejected("SELECT (", statement=0, reason_part="parse")
        self.assertFalse(report.readonly)

    def test_empty_input(self):
        self.assert_rejected("", statement=0, reason_part="no statement")
        self.assert_rejected("   -- only a comment", statement=0, reason_part="no statement")

    def test_report_is_empty_when_readonly(self):
        report = classify_readonly("SELECT 1", dialect="duckdb")
        self.assertEqual(report, (True, []))


if __name__ == "__main__":
    unittest.main()
