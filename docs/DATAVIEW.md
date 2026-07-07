# Dataview 引擎 — 技术设计 (PR 1)

> PR 1 (P0-1) 的技术设计文档。文档先于实现。

## 1. 设计目标

实现 Obsidian Dataview 风格的查询系统，从 `MultiLinkGraph` 自动算出 9 个核心指标，消除 `ARCHITECTURE.md` 第 10 章的代码缺位。

**核心约束**：
- 1 周可落地（极简 SQL 子集）
- 语法风格贴近 Obsidian Dataview（用户心智模型一致）
- 不做完整 SQL（避免 2-3 周过度工程）

## 2. 语法定义 (EBNF)

```ebnf
query       = select_clause from_clause [where_clause] [steps_clause] [order_clause] [limit_clause] ";" ;
select      = "SELECT" (field_list | agg_expr) ;
field_list  = field { "," field } ;
agg_expr    = agg "(" field ")" ;
agg         = "COUNT" | "AVG" | "SUM" | "MIN" | "MAX" ;
field       = IDENT | "*" ;
from        = "FROM" node_type ;
node_type   = IDENT ;                    (* 对应 NodeType enum 的 value: task/output/failure/...)
where       = "WHERE" condition ;
condition   = comparison | in_clause | steps_clause ;
comparison  = field op value ;
op          = "=" | "!=" | ">" | "<" | ">=" | "<=" ;
value       = STRING | NUMBER | IDENT ;
in_clause   = field "IN" "(" subquery ")" ;
subquery    = query ;
steps       = [NUMBER] "STEPS" "FROM" node_ref ;
node_ref    = IDENT | STRING ;
order       = "ORDER" "BY" field [("ASC" | "DESC")] ;
limit       = "LIMIT" NUMBER ;
```

**简化点**（不做）：
- ❌ JOIN（`WHERE field IN (subquery)` 替代）
- ❌ GROUP BY（`agg(field)` 直接用，隐式分组）
- ❌ 多表 FROM
- ❌ 复杂表达式 / 函数

## 3. 语法示例

### 3.1 基础查询

```sql
-- 列出所有 running 的 task
SELECT task, status, agent, started_at
FROM tasks
WHERE status = 'running'
ORDER BY started_at DESC
LIMIT 20;
```

### 3.2 图遍历（核心）

```sql
-- 当前 task 的 2 步邻居
SELECT task, status
FROM tasks
WHERE 2 STEPS FROM current_change;
```

### 3.3 聚合

```sql
-- 端到端成功率
SELECT COUNT(*) FILTER (WHERE status = 'completed') / COUNT(*) AS success_rate
FROM tasks;

-- 平均完成时间
SELECT AVG(duration)
FROM outputs
WHERE type = 'task_output';
```

### 3.4 子查询

```sql
-- 当前 task 类似失败怎么解决
SELECT AVG(success_rate)
FROM experiences
WHERE evolved_from IN (
    SELECT id FROM failures WHERE task_id = 'current_task'
);
```

### 3.5 9 个核心指标示例（demo.py 第 7 步）

```sql
-- 1. 端到端成功率
SELECT COUNT(*) FILTER (WHERE status = 'completed') / COUNT(*) AS success_rate FROM tasks;

-- 2. 平均完成时间（秒）
SELECT AVG(duration_seconds) FROM outputs WHERE type = 'task_output';

-- 3. 成本 / 任务
SELECT SUM(cost) / COUNT(*) AS cost_per_task FROM tasks;

-- 4. 用户满意度
SELECT AVG(score) FROM feedbacks WHERE type = 'user_rating';

-- 5. 失败率 (按 Agent)
SELECT agent, COUNT(*) FILTER (WHERE status = 'failed') / COUNT(*) AS fail_rate
FROM tasks GROUP BY agent;

-- 6. 反思触发率
SELECT COUNT(*) FROM reflections / COUNT(*) FROM failures;

-- 7. 升级请求率
SELECT COUNT(*) FROM escalations / COUNT(*) FROM failures;

-- 8. 校验失败率
SELECT COUNT(*) FILTER (WHERE valid = false) / COUNT(*) FROM outputs;

-- 9. 经验有效性
SELECT evolved_from_id, AVG(success_rate) FROM experiences GROUP BY evolved_from_id;
```

> 注：GROUP BY 在极简 SQL 子集中是**第二个里程碑**实现。第一版只支持 SELECT 全局聚合 + WHERE 过滤。

## 4. 数据模型

### 4.1 输入

```python
@dataclass
class QueryRequest:
    sql: str                          # SQL 字符串
    graph: MultiLinkGraph             # 当前图
    current_node: Optional[str] = None  # STEPS FROM 引用
```

### 4.2 输出

```python
@dataclass
class QueryResult:
    rows: List[Dict[str, Any]]        # 每行 = {field: value}
    columns: List[str]                 # 字段顺序
    aggregations: Dict[str, float]     # 聚合结果（COUNT/AVG/SUM）
    steps_executed: int                # 图遍历实际步数（用于性能监控）
    duration_ms: float                # 查询耗时
```

### 4.3 错误模型

```python
class QueryError(Exception):
    """查询解析或执行错误"""
    line: int
    column: int
    message: str
    hint: Optional[str] = None  # 给用户的修复建议
```

## 5. Python API

### 5.1 函数式（一次性查询）

```python
from agent_system.core.dataview import query

result = query("""
    SELECT COUNT(*) FILTER (WHERE status = 'completed') / COUNT(*) AS success_rate
    FROM tasks;
""", graph=current_graph)

print(result.aggregations["success_rate"])  # 0.87
```

### 5.2 Builder（链式，类型安全）

```python
from agent_system.core.dataview import Query

q = Query(graph).from_("tasks").where(status="completed").count()
result = q.execute()  # 8
```

> Builder 是 SQL 的薄包装；解析器走 SQL 路径。Builder 只为 IDE 自动补全。

### 5.3 MetricsCalculator 集成

```python
from agent_system.core.dataview import MetricsCalculator

calc = MetricsCalculator(graph)
metrics = calc.calculate_all()  # 9 个指标，自动用 Dataview 查询
```

> `MetricsCalculator` 是**当前实现**的兼容层。新指标通过 SQL 加，旧手算保留为 `_legacy_calculate()` 直到全量替换。

## 6. 核心算法

### 6.1 解析流程

```
SQL string
  ↓
Tokenizer (regex-based, ~100 行)
  ↓
Token stream
  ↓
Parser (recursive descent, ~300 行)
  ↓
AST (SelectStmt / FromClause / WhereClause / StepsClause)
  ↓
Validator (语义检查: 字段存在、类型匹配、STEPS FROM node 存在)
  ↓
QueryPlan
```

### 6.2 执行流程

```
QueryPlan
  ↓
Executor
  ├─ from_clause:  type_index[type] → candidate_nodes
  ├─ where_clause: filter candidates (comparison / IN / STEPS)
  │    └─ STEPS: BFS graph.neighbors() N 步
  ├─ select_clause:
  │    ├─ field_list → 返回 rows
  │    └─ agg_expr → 返回 aggregations
  ├─ order_clause: sort rows
  └─ limit_clause: take first N
  ↓
QueryResult
```

### 6.3 图遍历复用

`STEPS FROM node` 直接复用 `MultiLinkGraph.neighbors(node_id, depth=N, max_depth=N)` 已实现的 BFS。零重复实现。

## 7. 测试用例 (至少 12 个)

| # | 测试名 | 验证 |
|---|---|---|
| 1 | test_tokenizer_keywords | SQL 关键字识别 |
| 2 | test_tokenizer_identifiers | 字段名识别 |
| 3 | test_parse_select_from | 基础 SELECT/FROM 解析 |
| 4 | test_parse_where_comparison | WHERE =, !=, > 等 |
| 5 | test_parse_aggregation | COUNT/AVG/SUM |
| 6 | test_parse_steps_from | N STEPS FROM 解析 |
| 7 | test_parse_subquery | IN (subquery) |
| 8 | test_execute_simple_filter | 执行 SELECT + WHERE |
| 9 | test_execute_aggregation | COUNT/AVG 返回值 |
| 10 | test_execute_steps_traversal | 2 STEPS FROM 邻居 |
| 11 | test_execute_subquery | IN 子查询 |
| 12 | test_metrics_calculator_9 | 9 指标端到端 |
| 13 | test_error_messages | 解析错位置 + 提示 |
| 14 | test_performance_1000_nodes | 1000 节点 < 100ms |

## 8. 不做什么 (Scope Limit)

明确划清边界，避免后续 PR 蔓延：

- ❌ 完整 SQL（JOIN / 复杂子查询 / 窗口函数）—— PR 2+
- ❌ 实时流式更新（每次查询走最新图快照）—— PR 3+
- ❌ 索引优化（依赖 `graph._type_index`）—— PR 3+
- ❌ 缓存层（查询结果不缓存）—— PR 3+
- ❌ 权限隔离（不过滤租户）—— 由 `AccessControl` 上层做
- ❌ 写操作（INSERT / UPDATE / DELETE）—— 纯读查询引擎

## 9. 实现路径 (估时)

| 步骤 | 工作量 | 累计 |
|---|---|---|
| Tokenizer | 0.5 天 | 0.5 天 |
| Parser | 1 天 | 1.5 天 |
| Validator + Executor | 1.5 天 | 3 天 |
| 9 个指标 SQL 写 + 验证 | 0.5 天 | 3.5 天 |
| 测试 (12+ 用例) | 1 天 | 4.5 天 |
| demo.py 改造 + RUNBOOK 补充 | 0.5 天 | 5 天 |
| 总计 | **~1 周** | ✓ |

## 10. 风险与对策

| 风险 | 严重度 | 对策 |
|---|---|---|
| SQL 解析复杂度超估 | 中 | 严格按 EBNF 子集；超规格查询返回明确错误 |
| 图遍历性能差（1000+ 节点）| 中 | 复用 `MultiLinkGraph.neighbors()` BFS；加性能测试 |
| 与现有 `MetricsCalculator` 冲突 | 低 | 保留 `_legacy_calculate()` 兼容函数，渐近替换 |
| SQL 注入（虽然只读）| 低 | 用 token 流而非字符串拼接；值用 Pydantic 类型校验 |
| 测试数据 fixture 复杂 | 低 | `tests/fixtures/dataview_graph.json` 准备一个标准图 |

## 11. 交付物清单

- [ ] `src/agent_system/core/dataview.py` — 引擎主体
- [ ] `src/agent_system/memory/graph.py` — 增加 `query()` 方法（薄包装）
- [ ] `src/agent_system/observability/metrics.py` — 改为调 dataview
- [ ] `tests/test_dataview.py` — 12+ 测试
- [ ] `tests/fixtures/dataview_graph.json` — 测试 fixture
- [ ] `demo.py` — 第 7 步改造
- [ ] `docs/DATAVIEW.md` — 本文档（已完成）
- [ ] `docs/RUNBOOK.md` — 加 "9 指标如何查询" 章节
- [ ] `ARCHITECTURE.md` — 第 10 章勾选"已落地"

## 12. 验收复核

PR 1 完成定义（DoD）：
- [ ] 所有 12+ 测试通过
- [ ] demo.py 第 7 步跑通，9 个指标打印
- [ ] `pytest tests/test_dataview.py --cov=agent_system.core.dataview` 覆盖率 ≥ 80%
- [ ] ARCHITECTURE.md 第 10 章标注 "✅ Implemented in PR 1"
- [ ] PR 描述含：设计要点 / 验收标准 / 回滚步骤 / 测试结果截图