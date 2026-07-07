# TODO — v0.1.0 之后的修复与完善路线图

> 4 个 PR 各自独立可 revert。每个 PR 都有目标 / 文件 / 验收 / 回滚 / 测试。
> 排序依据：架构承诺 gap (P0) → 内部卫生 (P1) → 产品差异化 (P2)

## PR 1 — Dataview 引擎 (P0-1)

**优先级**：🔴 P0 — 架构文档最大 gap，对外立得住的关键

### 目标
实现 Obsidian Dataview 风格的查询系统，从 `MultiLinkGraph` 自动算出 9 个核心指标，**消除 ARCHITECTURE.md 第 10 章的代码缺位**。

### 文件清单
| 文件 | 操作 | 行数估算 |
|---|---|---|
| `src/agent_system/core/dataview.py` | 新建 | ~300 |
| `src/agent_system/memory/graph.py` | 增加 `query()` 方法 | +50 |
| `src/agent_system/observability/metrics.py` | 重写 `MetricsCalculator`，调用 dataview | 重写 |
| `tests/test_dataview.py` | 新建 | ~150 |
| `docs/DATAVIEW.md` | 新建（语法参考 + 9 指标示例）| ~100 |
| `demo.py` 第 7 步 | 改为调 dataview | +20 |

### 验收标准
- [ ] `dataview.query("SELECT ... FROM ... WHERE ... STEPS FROM ...")` 可解析并执行
- [ ] 9 个核心指标（端到端成功率 / 平均完成时间 / 成本 / 满意度 / 失败率 / 反思触发率 / 升级请求率 / 校验失败率 / 经验有效性）可自动算
- [ ] demo.py 跑通
- [ ] `test_dataview.py` 至少 12 个测试通过
- [ ] ARCHITECTURE.md 第 10 章已勾选（"借鉴 Obsidian Dataview 已落地"）

### 回滚方案
`git revert <commit>` 即可。`MetricsCalculator` 重写前保留旧实现为 `_legacy_calculate()` 兼容函数。

### 测试要求
- 单元：解析器 / 路径遍历 / 聚合函数
- 集成：demo.py 第 7 步跑通
- 文档：`docs/DATAVIEW.md` 含语法 + 9 指标 SQL 示例

---

## PR 2 — 拆 `execute()` 单方法 (P1-1)

**优先级**：🟡 P1 — 内部卫生，为后续 Mixin 重构铺路

### 目标
`core/agent.py:execute()` 当前 130+ 行 + 嵌套深，**加 Mixin 必崩**。拆为 3-4 个小方法。

### 文件清单
| 文件 | 操作 |
|---|---|
| `src/agent_system/core/agent.py` | 重构 `execute()` → `_setup_checkpoint()` + `_run_with_retry()` + `_handle_final_failure()` + `_escalate()` |
| `tests/test_iteration*.py` | 保持全绿（行为不变）|

### 验收标准
- [ ] `execute()` 主方法 ≤ 30 行
- [ ] 每个小方法单一职责，单元测试可独立 mock
- [ ] 现有 222 tests 全绿（行为不变）
- [ ] `git diff` 行数 -100 以内

### 回滚方案
纯重构，行为不变。`git revert` 无副作用。

### 测试要求
- 现有所有 iteration tests 必须保持全绿
- 不新增测试（行为不变原则）

---

## PR 3 — `_discover_peers` 用 registry (P1-2)

**优先级**：🟡 P1 — hardcoded 3 agent，加新 agent 必改

### 目标
`core/resolver.py:_discover_peers()` 当前 hardcode `ProductAgent` / `TechAgent` / `TestAgent` 三个类。改为**从 registry 动态发现**，新加 agent 不需要改 resolver。

### 文件清单
| 文件 | 操作 |
|---|---|
| `src/agent_system/core/resolver.py` | 重写 `_discover_peers()` 用 `AgentRegistry.discover()` |
| `src/agent_system/core/registry.py` | 新建（统一 agent / tool / mixin 注册中心）|
| `src/agent_system/agents/__init__.py` | 加 `@register_agent` decorator |
| `tests/test_resolver_peer_integration.py` | 增加测试覆盖 9 agent 全员被发现 |

### 验收标准
- [ ] `_discover_peers()` 返回当前所有 `@register_agent` 装饰的 agent
- [ ] 加新 agent 不需要改 resolver（验证：临时加一个 `FooAgent` 不报错）
- [ ] 现有 30 个 resolver 相关 tests 全绿
- [ ] `AgentRegistry` 与 `ToolRegistry` 接口一致（pattern 一致）

### 回滚方案
回滚后 resolver 退回 hardcode 模式（行为兼容，只是维护性下降）。

### 测试要求
- 至少 3 个新测试：registry discover / resolver 调用 / 加新 agent 自动可见

---

## PR 4 — Custom Agent 模板 (P2-1)

**优先级**：🟢 P2 — 产品差异化，依赖前面 3 个 PR

### 目标
实现 `agents/custom/` 平台，让用户定义自己的 Agent 而不碰主代码。

### 文件清单
| 文件 | 操作 |
|---|---|
| `src/agent_system/agents/custom/` | 从空目录实现 |
| `src/agent_system/agents/custom/base.py` | `CustomAgent` 抽象类 + YAML 加载 |
| `src/agent_system/agents/custom/loader.py` | 从 `agents/custom/*.yaml` 动态加载 |
| `examples/custom_agents/` | 1-2 个示例（translator / summarizer）|
| `tests/test_custom_agent.py` | 已存在空文件，填充测试 |
| `docs/CUSTOM_AGENT.md` | 用户文档 |

### 验收标准
- [ ] 用户可在 `examples/custom_agents/foo.yaml` 写一个 agent 定义，无需改 Python 代码
- [ ] `CustomAgent` 继承 `SmartAgent` 所有能力
- [ ] 至少 5 个测试通过
- [ ] 文档示例可一键跑通

### 回滚方案
新功能，回滚后 `agents/custom/` 目录删除，主代码零影响。

### 测试要求
- 单元：YAML 解析 / capability 校验 / agent 注册
- 集成：示例 agent 可端到端跑通

---

## 已完成

- [x] `fd44d5f` (2026-07-07) fix(peer): API key gate + autogen TaskResult import 兼容
- [x] `774517b` PEER path: AutoGen 0.4+ RoundRobinGroupChat replacing DiscussionMixin + DeepSeek LLM support
- [x] `ee86ac9` Initial release: Agent System v0.1.0
- [x] H1 — 清理工作区 (33 个 tmp/output 噪音文件已 trash)
- [x] **PR 1 — Dataview 引擎** (2026-07-07)
  - `src/agent_system/core/dataview.py` (~870 行：Tokenizer + Parser + Executor + Builder)
  - `src/agent_system/core/observability.py` (MetricsCalculator 改用 Dataview SQL)
  - `src/agent_system/memory/graph.py` (`graph.query()` 薄包装)
  - `demo.py` 第 7b 步（直接 Dataview SQL demo）
  - `tests/test_dataview.py` — **28 个测试全通过**
  - 9 个核心指标全可达

## 待办（卫生）

- [ ] **H0.5** — 网络恢复后 push 本地领先（`fd44d5f`）到 origin/main
- [ ] `.github/PR_BODY.md` 当前是 PEER PR 描述，发完 PR 后归档
- [ ] `git` 单文件确认是否 binary 或临时产物