"""
Migration-tool verification for GRANT/REVOKE transpilation between PostgreSQL
and Oracle, the two dialects with existing AST-level GRANT/REVOKE support.

The migration tool must preserve the meaning of WITH GRANT OPTION,
REVOKE GRANT OPTION FOR (revoking only the grant option) and column-level
privileges. When the target dialect cannot express a construct, generation
must halt with a rejection reason for the migration report instead of
silently emitting SQL with different semantics.

All tests are purely parse/generate round-trips; no database is needed.
"""

import unittest

import sqlglot
from sqlglot import exp


class GrantMigrationError(Exception):
    """Raised at generation time when the target dialect cannot express a grant."""


# Capability matrix of the dialects the migration tool converts between.
# Anything missing here must stop generation (fail closed).
# column_privileges: None means any privilege may carry a column list.
DIALECT_CAPABILITIES = {
    "postgres": {
        "grant_option": True,
        "revoke_grant_option_for": True,
        "revoke_cascade_restrict": True,
        "column_privileges": None,
        "securable_kinds": {
            "TABLE",
            "VIEW",
            "SEQUENCE",
            "FUNCTION",
            "PROCEDURE",
            "SCHEMA",
            "DATABASE",
            "TYPE",
        },
        "principal_kinds": {"ROLE", "GROUP"},
    },
    "oracle": {
        "grant_option": True,
        "revoke_grant_option_for": False,
        "revoke_cascade_restrict": False,
        "column_privileges": {"INSERT", "REFERENCES", "UPDATE"},
        "securable_kinds": {
            "TABLE",
            "VIEW",
            "SEQUENCE",
            "FUNCTION",
            "PROCEDURE",
            "PACKAGE",
            "TYPE",
        },
        "principal_kinds": set(),
    },
}


def migrate_grant(sql: str, source: str, target: str) -> str:
    """Convert one GRANT/REVOKE statement, halting if the target cannot express it."""
    tree = sqlglot.parse_one(sql, read=source)

    if not isinstance(tree, (exp.Grant, exp.Revoke)):
        raise GrantMigrationError(
            "statement is not a parseable GRANT/REVOKE (parsed as Command); "
            "manual migration required"
        )

    capabilities = DIALECT_CAPABILITIES[target]

    if tree.args.get("grant_option"):
        if isinstance(tree, exp.Grant) and not capabilities["grant_option"]:
            raise GrantMigrationError(f"target dialect '{target}' cannot express WITH GRANT OPTION")
        if isinstance(tree, exp.Revoke) and not capabilities["revoke_grant_option_for"]:
            raise GrantMigrationError(
                f"target dialect '{target}' cannot express REVOKE GRANT OPTION FOR "
                "(revoking only the grant option)"
            )

    if tree.args.get("cascade") and not capabilities["revoke_cascade_restrict"]:
        raise GrantMigrationError(
            f"target dialect '{target}' cannot express REVOKE ... CASCADE/RESTRICT"
        )

    for privilege in tree.args["privileges"]:
        column_privileges = capabilities["column_privileges"]
        if (
            privilege.expressions
            and column_privileges is not None
            and privilege.this.name not in column_privileges
        ):
            raise GrantMigrationError(
                f"target dialect '{target}' supports column lists only on "
                f"{', '.join(sorted(column_privileges))} privileges"
            )

    kind = tree.args.get("kind")
    if kind and kind.upper() not in capabilities["securable_kinds"]:
        raise GrantMigrationError(
            f"target dialect '{target}' cannot express grants on {kind.upper()} securables"
        )

    for principal in tree.args["principals"]:
        principal_kind = principal.args.get("kind")
        if principal_kind and principal_kind.upper() not in capabilities["principal_kinds"]:
            raise GrantMigrationError(
                f"target dialect '{target}' cannot express {principal_kind.upper()} principals"
            )

    return tree.sql(dialect=target)


def grant_signature(tree: exp.Expression):
    """Semantic fingerprint: privileges, securable, principals and options."""
    return (
        type(tree).__name__,
        tuple(
            (privilege.this.name, tuple(column.name for column in privilege.expressions))
            for privilege in tree.args["privileges"]
        ),
        tree.args.get("kind"),
        tuple((part.name, part.args.get("quoted", False)) for part in tree.args["securable"].parts),
        tuple(
            (
                principal.this.name,
                principal.this.args.get("quoted", False),
                principal.args.get("kind") or None,
            )
            for principal in tree.args["principals"]
        ),
        bool(tree.args.get("grant_option")),
        tree.args.get("cascade"),
    )


def migration_report(statements, source: str, target: str) -> dict:
    """Build the migration report: converted statements and rejections with reasons."""
    report = {"converted": [], "rejected": []}
    for statement in statements:
        try:
            report["converted"].append((statement, migrate_grant(statement, source, target)))
        except GrantMigrationError as error:
            report["rejected"].append((statement, str(error)))
    return report


POSTGRES_ROUNDTRIP = [
    "GRANT SELECT ON TABLE users TO role1",
    "GRANT INSERT, DELETE ON TABLE orders TO user1",
    "GRANT SELECT ON employees TO manager WITH GRANT OPTION",
    "GRANT ALL PRIVILEGES ON DATABASE mydb TO PUBLIC",
    "GRANT USAGE ON SCHEMA finance TO user2",
    "GRANT SELECT(id, name), UPDATE(email) ON customers TO customer_service",
    "GRANT SELECT ON t TO ROLE r1, GROUP g1",
    'GRANT SELECT ON "My Table" TO "User Name" WITH GRANT OPTION',
    'GRANT UPDATE("salary") ON "HR"."Employees" TO "Mgr"',
    "REVOKE SELECT ON TABLE users FROM role1",
    "REVOKE GRANT OPTION FOR SELECT ON employees FROM manager",
    "REVOKE USAGE ON SCHEMA finance FROM user2 CASCADE",
    "REVOKE SELECT ON TABLE orders FROM user1 RESTRICT",
    "REVOKE SELECT(id, name), UPDATE(email) ON customers FROM customer_service",
]

ORACLE_ROUNDTRIP = [
    "GRANT SELECT ON TABLE t TO maria, harry",
    "GRANT SELECT ON TABLE s.v TO PUBLIC",
    "GRANT UPDATE, TRIGGER ON TABLE t TO anita, zhi",
    "GRANT EXECUTE ON PROCEDURE p TO george",
    "GRANT USAGE ON SEQUENCE order_id TO sales_role",
    "GRANT SELECT ON t TO u WITH GRANT OPTION",
    "GRANT UPDATE(salary) ON employees TO hr",
    'GRANT INSERT("Weird Col") ON "T"."Obj" TO "User Name"',
    "REVOKE SELECT ON TABLE t FROM maria, harry",
    "REVOKE UPDATE(salary) ON employees FROM hr",
]

# (source dialect, statement, expected target statement)
VALID_MIGRATIONS = [
    # postgres -> oracle
    ("postgres", "GRANT SELECT ON TABLE users TO role1", "GRANT SELECT ON TABLE users TO role1"),
    (
        "postgres",
        "GRANT SELECT ON employees TO manager WITH GRANT OPTION",
        "GRANT SELECT ON employees TO manager WITH GRANT OPTION",
    ),
    (
        "postgres",
        "GRANT UPDATE(salary) ON employees TO hr_manager",
        "GRANT UPDATE(salary) ON employees TO hr_manager",
    ),
    (
        "postgres",
        "GRANT INSERT, DELETE ON TABLE orders TO user1",
        "GRANT INSERT, DELETE ON TABLE orders TO user1",
    ),
    (
        "postgres",
        'GRANT SELECT ON "My Table" TO "User Name"',
        'GRANT SELECT ON "My Table" TO "User Name"',
    ),
    (
        "postgres",
        "REVOKE SELECT ON TABLE users FROM role1",
        "REVOKE SELECT ON TABLE users FROM role1",
    ),
    (
        "postgres",
        "REVOKE UPDATE(salary) ON employees FROM hr_manager",
        "REVOKE UPDATE(salary) ON employees FROM hr_manager",
    ),
    # oracle -> postgres
    (
        "oracle",
        "GRANT SELECT ON TABLE t TO maria, harry",
        "GRANT SELECT ON TABLE t TO maria, harry",
    ),
    ("oracle", "GRANT EXECUTE ON PROCEDURE p TO george", "GRANT EXECUTE ON PROCEDURE p TO george"),
    (
        "oracle",
        "GRANT USAGE ON SEQUENCE order_id TO sales_role",
        "GRANT USAGE ON SEQUENCE order_id TO sales_role",
    ),
    (
        "oracle",
        'GRANT INSERT("Weird Col") ON "T"."Obj" TO "User Name"',
        'GRANT INSERT("Weird Col") ON "T"."Obj" TO "User Name"',
    ),
    (
        "oracle",
        "REVOKE SELECT ON TABLE s.v FROM PUBLIC",
        "REVOKE SELECT ON TABLE s.v FROM PUBLIC",
    ),
]

# (source dialect, statement, expected rejection reason)
REJECTED_MIGRATIONS = [
    (
        "postgres",
        "REVOKE GRANT OPTION FOR SELECT ON employees FROM manager",
        "target dialect 'oracle' cannot express REVOKE GRANT OPTION FOR "
        "(revoking only the grant option)",
    ),
    (
        "postgres",
        "REVOKE SELECT ON t FROM u CASCADE",
        "target dialect 'oracle' cannot express REVOKE ... CASCADE/RESTRICT",
    ),
    (
        "postgres",
        "REVOKE SELECT ON t FROM u RESTRICT",
        "target dialect 'oracle' cannot express REVOKE ... CASCADE/RESTRICT",
    ),
    (
        "postgres",
        "GRANT SELECT(id, name) ON customers TO customer_service",
        "target dialect 'oracle' supports column lists only on "
        "INSERT, REFERENCES, UPDATE privileges",
    ),
    (
        "postgres",
        "GRANT ALL PRIVILEGES ON DATABASE mydb TO PUBLIC",
        "target dialect 'oracle' cannot express grants on DATABASE securables",
    ),
    (
        "postgres",
        "GRANT USAGE ON SCHEMA finance TO user2",
        "target dialect 'oracle' cannot express grants on SCHEMA securables",
    ),
    (
        "postgres",
        "GRANT SELECT ON t TO GROUP g1",
        "target dialect 'oracle' cannot express GROUP principals",
    ),
    (
        "postgres",
        "GRANT admin_role TO mary WITH ADMIN OPTION",
        "statement is not a parseable GRANT/REVOKE (parsed as Command); "
        "manual migration required",
    ),
    (
        "oracle",
        "GRANT purchases_reader_role TO george, maria",
        "statement is not a parseable GRANT/REVOKE (parsed as Command); "
        "manual migration required",
    ),
]


class TestGrantRoundTrip(unittest.TestCase):
    def assert_roundtrip(self, sql, dialect):
        tree = sqlglot.parse_one(sql, read=dialect)
        self.assertIsInstance(tree, (exp.Grant, exp.Revoke))
        generated = tree.sql(dialect=dialect)
        self.assertEqual(generated, sql)
        reparsed = sqlglot.parse_one(generated, read=dialect)
        self.assertEqual(grant_signature(reparsed), grant_signature(tree))

    def test_postgres_roundtrip(self):
        for sql in POSTGRES_ROUNDTRIP:
            with self.subTest(sql=sql):
                self.assert_roundtrip(sql, "postgres")

    def test_oracle_roundtrip(self):
        for sql in ORACLE_ROUNDTRIP:
            with self.subTest(sql=sql):
                self.assert_roundtrip(sql, "oracle")


class TestGrantMigration(unittest.TestCase):
    def test_valid_migrations(self):
        for source, sql, expected in VALID_MIGRATIONS:
            target = "oracle" if source == "postgres" else "postgres"
            with self.subTest(sql=sql):
                migrated = migrate_grant(sql, source, target)
                self.assertEqual(migrated, expected)
                source_signature = grant_signature(sqlglot.parse_one(sql, read=source))
                target_signature = grant_signature(sqlglot.parse_one(migrated, read=target))
                self.assertEqual(source_signature, target_signature)

    def test_rejected_migrations(self):
        for source, sql, reason in REJECTED_MIGRATIONS:
            target = "oracle" if source == "postgres" else "postgres"
            with self.subTest(sql=sql):
                with self.assertRaises(GrantMigrationError) as ctx:
                    migrate_grant(sql, source, target)
                self.assertEqual(str(ctx.exception), reason)

    def test_migration_report(self):
        statements = [sql for source, sql, _ in VALID_MIGRATIONS if source == "postgres"]
        statements += [sql for source, sql, _ in REJECTED_MIGRATIONS if source == "postgres"]

        report = migration_report(statements, "postgres", "oracle")

        self.assertEqual(
            report["converted"],
            [(sql, expected) for source, sql, expected in VALID_MIGRATIONS if source == "postgres"],
        )
        self.assertEqual(
            report["rejected"],
            [(sql, reason) for source, sql, reason in REJECTED_MIGRATIONS if source == "postgres"],
        )


if __name__ == "__main__":
    unittest.main()
