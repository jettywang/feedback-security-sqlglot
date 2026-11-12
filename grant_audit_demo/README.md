# SQLGlot AST diff 接入数据库权限变更审核 —— 可行性评估

环境：仓库内 sqlglot `0.0.1.dev2`，方言默认 `postgres`。所有结论由
`grant_diff_demo.py` 与 `grant_review.py` 实测得出，可重复运行：

```bash
python grant_audit_demo/grant_diff_demo.py
python grant_audit_demo/grant_review.py
```

## 1. diff 引擎对 GRANT/REVOKE 的三个结构性约束

`Grant` 节点结构（`sqlglot/expressions/query.py:628`）：

```text
Grant(privileges=[GrantPrivilege(...)], kind, securable=Table(...),
      principals=[GrantPrincipal(this=Identifier(...), kind)],
      grant_option: bool)
Revoke = Grant 字段 + cascade: bool
```

结合 `sqlglot/diff.py` 的实现，有三条直接决定可靠性的规则：

1. **只有白名单类型能产生 `Update`**（`diff.py:151` 的
   `UPDATABLE_EXPRESSION_TYPES`：Alias/Boolean/Column/DataType/Lambda/
   Literal/Table/Window）。`Grant`、`Revoke`、`GrantPrivilege`、
   `GrantPrincipal` 都不在其中，它们的变化只能体现为节点级
   `Remove`/`Insert`，或因非表达式参数（布尔位）变化产生节点 `Update`。
2. **`Identifier` 被整体排除在 diff 索引外**（`diff.py:162`
   `IGNORED_LEAF_EXPRESSION_TYPES`）。因此只包含一个 Identifier 的
   `GrantPrincipal` 在主体改名时没有任何可匹配叶子，diff 直接给 `Keep`。
3. **叶子匹配是 bigram Dice 相似度（阈值 0.6）而非严格相等**（`diff.py:339`
   起），节点类型不同（Grant vs Revoke）根本不进入匹配。

## 2. 四类目标变化的实测结论

| 变化 | 实测 edit 结果 | 可靠性 | 说明 |
|---|---|---|---|
| 同一条 GRANT 改对象名 `t1→t2` | `Update: Table(db.t1) => Table(db.t2)` | 可靠 | Table 在 Update 白名单，可直接定位对象 |
| 仅格式变化（大小写/空白/分号） | 零 edit | 可靠（符合预期） | 词法层面被规范化 |
| 仅加引号 `t1→"t1"` | `Update: Table(...)` | **误报** | 标识符文本相同但 `quoted` 位不同，需方言折叠判断是否同一对象 |
| GRANT 与 REVOKE 调换 | `Remove Grant` + `Insert Revoke` + 子节点 `Move` | 可靠但需解释 | 类型不同无法配对；Move 不代表语义移动，审核页应只认动作翻转 |
| 转授权选项变化 | `Update: Grant(...)`（grant_option 布尔位） | 可靠 | 但 edit 不指明是哪个字段，需读 `args['grant_option']` |
| 主体名变化 `r1→r2` | **零 edit（盲区）** | 不可靠 | Identifier 被忽略，GrantPrincipal 报 Keep |
| 权限变化 `SELECT→DELETE` | `Remove GrantPrivilege` + `Insert GrantPrivilege` | 可靠 | 非白名单类型，以增删形式呈现 |
| 列级权限 `col1→col2` | `Update: Column(col1) => Column(col2)` | 可靠 | Column 在白名单 |
| 迁移脚本内语句顺序调换 | 规范化配对后零差异 | 可靠 | `sql()` 规范化后做集合配对，顺序天然无关 |

### 最小迁移脚本与真实 edit 输出

```sql
-- A. 对象名变化
GRANT SELECT ON TABLE db.t1 TO ROLE r1;
GRANT SELECT ON TABLE db.t2 TO ROLE r1;
-- => Update: Table(db.t1) => Table(db.t2)

-- B. 仅格式变化
grant select on table db.t1 to role r1;
GRANT SELECT  ON TABLE db.t1 TO ROLE r1;
-- => (no edits)

-- C. GRANT/REVOKE 调换（同一权限主体对象）
GRANT  SELECT ON TABLE db.t1 TO ROLE r1;
REVOKE SELECT ON TABLE db.t1 FROM ROLE r1;
-- => Remove Grant / Insert Revoke / Move 三个公共子树

-- D. 转授权选项
GRANT SELECT ON TABLE db.t1 TO ROLE r1;
GRANT SELECT ON TABLE db.t1 TO ROLE r1 WITH GRANT OPTION;
-- => Update: Grant(... grant_option False=>True)
```

## 3. 方言标识符规则与所需的额外上下文

实测 postgres / snowflake / mysql / bigquery：

- 未加引号的标识符 AST 只保留原始文本（`Identifier(name, quoted=False)`），
  **sqlglot 不做方言级大小写折叠**。Postgres/Snowflake 中未引号名在目录里
  折叠为大写（Snowflake）或小写（Postgres），`t1` 与 `T1` 是否同对象必须由
  目标库的 catalog/元数据判定，AST 给不出答案。
- 加引号后 `quoted=True`，与未引号形式即使文本相同也会让 Table 比较失败
  （产生场景 2b 的误报 Update）。审核层需按方言做一次名称归一化再比对。
- MySQL 生成器会把双引号改写为反引号 `` `t1` ``，跨方言 diff 前必须固定
  `read=`/生成方言；同一条 GRANT 在 snowflake 与 postgres 下解析出的 AST
  实测相等，但不要依赖这一点处理带引号或保留字标识符。
- `GRANT ... TO USER r1` 在当前版本各方言均**解析降级为 `Command`**
  （stderr 有 fallback 警告），拿不到 Grant 节点；`TO r1` 则解析为
  `kind=False` 的 GrantPrincipal。审核层必须检测 `Command` 降级并转人工。

## 4. 审核页面接入建议

页面需要的三个定位维度，建议不要直接渲染原始 edit，而是先抽结构化三元组
（见 `grant_review.py` 的 `extract_triplets`）：

```text
(action=GRANT|REVOKE, principal=KIND:name, object=catalog.db.table,
 privileges=(...), grant_option=bool)
```

- **主体（principal）**：不能依赖 diff，直接比对三元组；diff 零 edit 但
  三元组不同即标记 `principal_change` 高危。
- **对象（object）**：消费 Table 的 Update，但先做方言引号归一化；
  名称相同仅 quoted 位不同标记 `quote_only_change`，交 catalog 确认。
- **权限（privilege）**：把 GrantPrivilege 的 Remove/Insert 归并为
  “移除权限 X / 新增权限 Y”；列级权限可直接用 Column Update 定位列。
- **动作翻转**：Grant/Revoke 的 Remove+Insert 对展示为“授权→收权”，
  忽略其中的 Move 噪音。
- **转授权**：读 `Grant.args['grant_option']`（Revoke 另有 `cascade`），
  展示布尔位前后值，而不是整节点 Update 文本。

### 必须标注“仅凭 AST 无法确定”的变更

1. `principal_change`：diff 完全无信号，靠三元组兜底，但 ROLE/USER 是否
   指向同一主体仍需 IAM/目录上下文。
2. `quote_only_change` 及任何未引号大小写差异：是否同一数据库对象取决于
   方言折叠规则与实际 catalog。
3. `unparsed_statement`：降级为 Command 的语句（如本版本 `TO USER`），
   AST 不含任何权限结构，必须人工或换专用解析器。
4. 同名对象在不同 schema/catalog 的歧义、权限的实际生效结果（REVOKE
   CASCADE 的连锁效果、GRANT OPTION 传播链）：AST 只描述语句意图，
   不描述运行时状态，需对接权限系统快照。

## 5. 结论

sqlglot AST diff 适合作为审核流水线的**结构化第一层**：对象名、权限增删、
列级权限、GRANT/REVOKE 翻转、grant option、格式噪音过滤都能可靠覆盖；
但主体改名是确定性盲区，引号/大小写语义和 Command 降级必须引入方言归一化
与 catalog 上下文。建议以“三元组比对为主、AST edit 为定位证据、歧义标记
转人工”的组合方式落地，而不是直接把 edit script 展示给审核人。
