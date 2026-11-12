"""
权限变更审核器：在 sqlglot.diff 之上补一层结构化三元组比对，
用于审核页面定位 主体 / 对象 / 权限，并标注 AST 无法独立判定的变更。

运行: python grant_audit_demo/grant_review.py
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp, parse, parse_one
from sqlglot.diff import diff

DIALECT = "postgres"

# 仅凭 sqlglot.diff 的 edit script 无法可靠判定结果的变更类别
AST_ONLY_AMBIGUOUS = {
    "principal_change",   # Identifier 被 diff 忽略，主体名变化不产生 edit
    "quote_only_change",  # quoted 标志触发 Update，但方言折叠后可能是同一对象
    "unparsed_statement", # 解析降级为 Command，没有结构化 AST
}


@dataclass(frozen=True)
class PermissionTriplet:
    action: str          # GRANT / REVOKE / UNPARSED
    principal: str       # 例如 ROLE:r1
    object: str          # 规范化对象名，例如 db.t1
    privileges: tuple[str, ...]
    grant_option: bool

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.action, self.principal, self.object)


def extract_triplets(sql: str, dialect: str = DIALECT) -> list[PermissionTriplet]:
    statement = parse_one(sql, read=dialect)
    if not isinstance(statement, (exp.Grant, exp.Revoke)):
        return [
            PermissionTriplet("UNPARSED", type(statement).__name__, sql, (), False)
        ]

    table = statement.find(exp.Table)
    object_name = table.sql(dialect=dialect) if table else "?"
    privileges = tuple(
        node.this.name if node.this else "*"
        for node in statement.find_all(exp.GrantPrivilege)
    )

    principals = list(statement.find_all(exp.GrantPrincipal)) or [None]
    triplets = []
    for principal in principals:
        if principal is None:
            principal_name = "?"
        else:
            identifier = principal.find(exp.Identifier)
            kind = principal.args.get("kind") or ""
            principal_name = f"{kind}:{identifier.name if identifier else principal.sql()}"
        triplets.append(
            PermissionTriplet(
                action=type(statement).__name__.upper(),
                principal=principal_name,
                object=object_name,
                privileges=privileges,
                grant_option=bool(statement.args.get("grant_option")),
            )
        )
    return triplets


def review_change(old_sql: str, new_sql: str, dialect: str = DIALECT) -> dict:
    old = parse_one(old_sql, read=dialect)
    new = parse_one(new_sql, read=dialect)
    edits = diff(old, new, delta_only=True) if type(old) is type(new) else None
    old_triplets = extract_triplets(old_sql, dialect)
    new_triplets = extract_triplets(new_sql, dialect)

    flags: set[str] = set()
    if any(t.action == "UNPARSED" for t in old_triplets + new_triplets):
        flags.add("unparsed_statement")
    if edits is not None:
        if not edits and old_triplets != new_triplets:
            flags.add("principal_change")
        if any(
            type(edit).__name__ == "Update"
            and isinstance(getattr(edit, "source", None), exp.Table)
            and edit.source.name == edit.target.name
            for edit in edits
        ):
            flags.add("quote_only_change")

    return {
        "old": old_sql,
        "new": new_sql,
        "ast_edits": [
            f"{type(edit).__name__}: {describe(edit, dialect)}"
            for edit in (edits or [])
        ],
        "old_triplets": old_triplets,
        "new_triplets": new_triplets,
        "ambiguous": sorted(flags),
        "requires_context": bool(flags),
    }


def describe(edit, dialect: str) -> str:
    if type(edit).__name__ == "Update":
        return f"{edit.source.sql(dialect=dialect)} => {edit.target.sql(dialect=dialect)}"
    node = getattr(edit, "expression", None) or edit.source
    return node.sql(dialect=dialect)


def review_scripts(old_script: str, new_script: str, dialect: str = DIALECT) -> list[dict]:
    old_map = {s.sql(dialect=dialect): s for s in parse(old_script, read=dialect) if s}
    new_map = {s.sql(dialect=dialect): s for s in parse(new_script, read=dialect) if s}
    results = []
    for key in sorted(old_map.keys() - new_map.keys()):
        results.append(review_change(old_map[key].sql(dialect=dialect), "", dialect))
    for key in sorted(new_map.keys() - old_map.keys()):
        results.append(review_change("", new_map[key].sql(dialect=dialect), dialect))
    return results


if __name__ == "__main__":
    cases = [
        ("对象名变化",
         "GRANT SELECT ON TABLE db.t1 TO ROLE r1",
         "GRANT SELECT ON TABLE db.t2 TO ROLE r1"),
        ("主体名变化（diff 盲区）",
         "GRANT SELECT ON TABLE db.t1 TO ROLE r1",
         "GRANT SELECT ON TABLE db.t1 TO ROLE r2"),
        ("仅加引号",
         "GRANT SELECT ON TABLE db.t1 TO ROLE r1",
         'GRANT SELECT ON TABLE db."t1" TO ROLE r1'),
        ("转授权选项变化",
         "GRANT SELECT ON TABLE db.t1 TO ROLE r1",
         "GRANT SELECT ON TABLE db.t1 TO ROLE r1 WITH GRANT OPTION"),
        ("GRANT 改 REVOKE",
         "GRANT SELECT ON TABLE db.t1 TO ROLE r1",
         "REVOKE SELECT ON TABLE db.t1 FROM ROLE r1"),
        ("不支持的语法降级",
         "GRANT SELECT ON TABLE db.t1 TO ROLE r1",
         "GRANT SELECT ON TABLE db.t1 TO USER r1"),
    ]
    for title, old_sql, new_sql in cases:
        result = review_change(old_sql, new_sql)
        print("=" * 78)
        print(title)
        print("  AST edits:", result["ast_edits"] or "(none)")
        print("  主体/对象/权限:")
        for triplet in result["old_triplets"]:
            print("   -", triplet)
        for triplet in result["new_triplets"]:
            print("   +", triplet)
        print("  需额外上下文:", result["ambiguous"] or "否")
