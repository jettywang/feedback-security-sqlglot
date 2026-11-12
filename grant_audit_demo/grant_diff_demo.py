"""
GRANT/REVOKE 变更审核最小演示：基于 sqlglot.diff 评估权限变更的可识别边界。

运行: python grant_audit_demo/grant_diff_demo.py
依赖: 当前环境已安装的 sqlglot（仓库工作区版本）。
"""
from __future__ import annotations

import sqlglot
from sqlglot import diff as diff_mod
from sqlglot import exp, parse, parse_one
from sqlglot.diff import diff

DIALECT = "postgres"


def render(node: exp.Expr) -> str:
    return f"{type(node).__name__}({node.sql(dialect=DIALECT)})"


def show_edits(title: str, old_sql: str, new_sql: str, dialect: str | None = None) -> None:
    print("=" * 78)
    print(title)
    print(f"  A(old): {old_sql}")
    print(f"  B(new): {new_sql}")
    old = parse_one(old_sql, read=dialect)
    new = parse_one(new_sql, read=dialect)
    edits = diff(old, new, delta_only=True)
    if not edits:
        print("  -> (no edits — AST 层面无差异)")
    for edit in edits:
        tag = type(edit).__name__
        if tag == "Update":
            print(f"  -> {tag}: {render(edit.source)}  =>  {render(edit.target)}")
        else:
            node = getattr(edit, "expression", None) or edit.source
            print(f"  -> {tag}: {render(node)}")


def statement_pair_review(old_script: str, new_script: str) -> None:
    """迁移脚本审核：先按规范化 SQL 配对语句，再对配对语句跑 AST diff。"""
    print("=" * 78)
    print("迁移脚本配对审核（语句顺序无关）")
    old_stmts = {s.sql(dialect=DIALECT): s for s in parse(old_script, read=DIALECT) if s}
    new_stmts = {s.sql(dialect=DIALECT): s for s in parse(new_script, read=DIALECT) if s}
    unchanged = old_stmts.keys() & new_stmts.keys()
    print(f"  未变化语句: {len(unchanged)} 条（顺序调换不算变化）")
    for key in sorted(unchanged):
        print(f"    keep: {key}")
    for key in sorted(old_stmts.keys() - new_stmts.keys()):
        print(f"    remove: {old_stmts[key].sql()}  [类型 {type(old_stmts[key]).__name__}]")
    for key in sorted(new_stmts.keys() - old_stmts.keys()):
        print(f"    insert: {new_stmts[key].sql()}  [类型 {type(new_stmts[key]).__name__}]")


if __name__ == "__main__":
    print("sqlglot version:", sqlglot.__version__)

    # 1) 同一条 GRANT 改了对象名 —— 可靠识别（Table 在可 Update 白名单内）
    show_edits(
        "场景1 对象名 t1 -> t2",
        "GRANT SELECT ON TABLE db.t1 TO ROLE r1",
        "GRANT SELECT ON TABLE db.t2 TO ROLE r1",
        DIALECT,
    )

    # 2a) 仅格式变化（大小写/空白/分号）—— 零 edit
    show_edits(
        "场景2a 仅大小写与空白变化",
        "grant select on table db.t1 to role r1",
        "GRANT SELECT  ON TABLE db.t1 TO ROLE r1;",
        DIALECT,
    )

    # 2b) 仅加了引号 —— 会产生误报 Update（quoted 标志属于被忽略的 Identifier 之外的 Table 比较）
    show_edits(
        "场景2b 仅加双引号（语义相同，注意误报）",
        "GRANT SELECT ON TABLE db.t1 TO ROLE r1",
        'GRANT SELECT ON TABLE db."t1" TO ROLE r1',
        DIALECT,
    )

    # 3) GRANT 与 REVOKE 调换 —— 节点类型不同，整体 Remove + Insert
    show_edits(
        "场景3 GRANT -> REVOKE",
        "GRANT SELECT ON TABLE db.t1 TO ROLE r1",
        "REVOKE SELECT ON TABLE db.t1 FROM ROLE r1",
        DIALECT,
    )

    # 4) 转授权选项变化 —— grant_option 是非表达式布尔参数，Grant 节点 Update
    show_edits(
        "场景4 WITH GRANT OPTION 增加/移除",
        "GRANT SELECT ON TABLE db.t1 TO ROLE r1",
        "GRANT SELECT ON TABLE db.t1 TO ROLE r1 WITH GRANT OPTION",
        DIALECT,
    )

    # 5) 主体名变化 —— 盲区：Identifier 被 diff 忽略，GrantPrincipal 报 Keep
    show_edits(
        "场景5 主体 r1 -> r2（AST diff 盲区，需要补充上下文比对）",
        "GRANT SELECT ON TABLE db.t1 TO ROLE r1",
        "GRANT SELECT ON TABLE db.t1 TO ROLE r2",
        DIALECT,
    )

    # 6) 权限变化 —— GrantPrivilege 不在 Update 白名单，表现为 Remove + Insert
    show_edits(
        "场景6 权限 SELECT -> DELETE",
        "GRANT SELECT ON TABLE db.t1 TO ROLE r1",
        "GRANT DELETE ON TABLE db.t1 TO ROLE r1",
        DIALECT,
    )

    # 7) 列级权限 —— Column 在 Update 白名单，可精确定位
    show_edits(
        "场景7 列级权限 col1 -> col2",
        "GRANT SELECT (col1) ON TABLE db.t1 TO ROLE r1",
        "GRANT SELECT (col2) ON TABLE db.t1 TO ROLE r1",
        DIALECT,
    )

    # 8) 迁移脚本中 GRANT/REVOKE 顺序调换 —— 规范化配对后顺序无关
    statement_pair_review(
        "GRANT SELECT ON TABLE db.t1 TO ROLE r1;"
        "REVOKE UPDATE ON TABLE db.t1 FROM ROLE r2;",
        "REVOKE UPDATE ON TABLE db.t1 FROM ROLE r2;"
        "GRANT SELECT ON TABLE db.t1 TO ROLE r1;",
    )
