# 企业级多 Agent 系统架构 v15.1

> **⚠️ HISTORICAL DESIGN DOCUMENT — last edited before v0.1.0**
>
> This file is the original v1–v15.1 design history, frozen at
> 51KB. For the **current code-level architecture** (modules,
> data flow, integration points), see:
> - [README.md](README.md#project-structure) — current module tree
>   and feature inventory
> - [docs/PRODUCTION.md](docs/PRODUCTION.md) — deployment architecture
>   and operational concerns
> - [docs/adr/](docs/adr/README.md) — table of contents for the
>   4 ADRs (RS256 JWT, PostgreSQL RLS, GitHub webhook) that
>   document the *why* of each major v0.2.0 / v0.3.0 decision
>
> If you want a full pass to bring this file up to date with the
> v0.3.0 code, see [`docs/TODO.md`](docs/TODO.md) §"Known technical
> debt — ARCHITECTURE.md".

完整的架构设计文档，整合 v1-v15.1 所有讨论。

> **v15.1 关键变化**：
> - **决策级别重构** — 人 > CEO Agent > 子 Agent，升级路径 4 选 1（SELF/PEER/HUMAN/ESCALATE）
> - **上下文隔离** — 6 种空间（私有/权限组/群组/项目/外部客户/租户公开）
> - **失败 UX** — 5 阶段 + 半成品保留 + 一键重试
> - **时间处理** — UTC 存储 + 工作时间 + 节假日 + 超时策略
> - **协作冲突** — 锁机制 + 乐观锁 + 优先级队列 + 冲突解决

---

## 文档结构

```
第 1 章  系统概览
第 2 章  核心设计理念
第 3 章  完整架构图（7+1 层）
第 4 章  关键子系统详解
第 5 章  智能升级机制（4 选 1，决策级别重构）
第 6 章  MCP 工具集成
第 7 章  数据与事件流
第 8 章  多向链接图系统 (v13)
第 9 章  插件化工具系统 (v13)
第 10 章 Dataview 风格查询 (v13)
第 11 章 上下文隔离
第 12 章 失败 UX
第 13 章 时间处理
第 14 章 协作冲突
第 15 章 技术栈
第 16 章 实施路径
第 17 章 风险与对策
附录 A   项目来源
附录 B   术语表
附录 C   借鉴 Obsidian 的设计
```

---

## 第 1 章 系统概览

### 1.1 这是什么

一个**企业级多 Agent 协作平台**，让公司各部门（产品/技术/测试/部署）都能用 AI 自动化日常工作。

**目标**：
- 把员工从重复劳动中解放
- 让 AI 工具之间能**协作**（不是各干各的）
- 系统能**自我改进**（失败 → 学经验 → 避免再犯）
- 人和 AI 的责任**清晰**（AI 能做的让人放心，AI 做不到的有人兜底）

### 1.2 适用场景

**适合**：
- 产品功能开发的全流程（需求 → 设计 → 实现 → 测试 → 部署）
- 重复性任务（周报、报告、文档整理）
- 跨部门协作（信息传递、知识沉淀）
- 自动化测试（UI + API）

**不适合**：
- 涉及法律/医疗/金融的最终决策
- 不可逆操作（删数据、转账）
- 涉及隐私/机密的处理

### 1.3 核心数据

| 项目 | 数值 |
|---|---|
| 支持 Agent 数量 | 5-20 个（可扩展） |
| 支持 MCP 工具 | 任意数量（热插拔） |
| LLM 选型 | 每 Agent 独立配置 |
| 部署方式 | Docker / K8s |
| 估计成本 | $20-100/月（小团队） |
| 上线时间 | 3-6 月（分阶段） |

---

## 第 2 章 核心设计理念

### 2.1 4 个核心原则

#### 原则 1：业务逻辑与基础设施分离

**业务逻辑**（写 PRD、写代码、写测试）= 短，可读
**基础设施**（升级、记忆、错误处理）= 长，复杂

→ 把基础设施做成**通用机制**（Mixin/中间件），所有 Agent 自动获得

#### 原则 2：智能协作而非盲目升级

**Agent 失败** → **不直接找 CEO**
- 路径 A：自己解决（最有信心时）
- 路径 B：找同事讨论（中等信心）
- 路径 C：升级 CEO（真的搞不定）

→ **减少 CEO 负担，让 Agent 学会协作**

#### 原则 3：标准化产出而非自由发挥

**所有 Agent 产出** = **JSON**（不是 markdown）
- 必填字段：id / type / created_at / schema_version / next_steps
- 通过 Schema 校验门 → 才能给下个 Agent
- 下个 Agent **机器可读**地消费

#### 原则 4：自我改进而非一成不变

**每次失败** → 自动反思 → 抽象成通用规则
**每次成功** → 提炼最佳实践
**所有经验** → 跨 Agent 共享

→ 系统**越用越聪明**

### 2.2 6 个核心创新

1. **抽象的通用机制**（SmartAgentMixin）—— 每个 Agent 自动有智能能力
2. **智能升级 4 选 1**（SELF/PEER/HUMAN/ESCALATE）—— 人 > CEO Agent > 子 Agent
3. **MCP 标准化** —— 工具接入零成本
4. **多向链接图** (v13) —— 超越双链的记忆系统
5. **插件化工具系统** (v13) —— 借鉴 Obsidian 插件架构
6. **上下文隔离** (v15.1) —— 6 种空间，多部门/项目/客户隔离

### 2.3 关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 主流程框架 | **LangGraph** | 状态管理、持久化、可视化最强 |
| 内部协作 | **AutoGen** | 多 Agent 群聊最自然 |
| 记忆系统 | **多向链接图** | 比双链强 10 倍 |
| 工具架构 | **插件化 (Obsidian 风格)** | 加新工具零成本 |
| LLM 选型 | **Claude 主用 + 多模型** | 主任务用 Sonnet，简单任务用 Haiku |
| 工具协议 | **MCP** | 标准化工具接入，避免重复造轮子 |
| 数据存储 | **Postgres + Chroma + Redis + JSON** | 关系 + 向量 + 缓存 + 文件 |
| 部署 | **Docker 起步** | 简单，K8s 后续 |

---

## 第 3 章 完整架构图（7+1 层）

### 3.1 完整 7+1 层架构

```
┌──────────────────────────────────────────────────────────────────────┐
│  L0 🛡️ 产品体验层                                                     │
│  - 错误友好化 (4 级错误: 临时/可恢复/业务/系统)                  │
│  - 任务超时与取消                                                     │
│  - 输入校验 (网关前置)                                              │
│  - 多语言 (i18n)                                                      │
│  - 能力边界文档 (AI 干啥/不干啥)                                    │
│  - 用户反馈组件 (显式+隐式 → 进反思系统)                            │
│  来自: 自建                                                           │
└───────────────────────────────────┬──────────────────────────────────┘
                                    │
                                    ↓
┌───────────────────────────────────▼────────────────────────────────┐
│  L1 👥 用户接入层 + 用户反馈                                          │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐    │
│  │ Web 控制台  │  │ IM 机器人   │  │ IDE 插件   │  │  移动端    │    │
│  └────────────┘  └────────────┘  └────────────┘  └────────────┘    │
│  来自: 自建前端 (React) + 第三方 IM 集成                            │
└───────────────────────────────────┬──────────────────────────────────┘
                                    │
                                    ↓
┌───────────────────────────────────▼────────────────────────────────┐
│  L2 🚪 API 网关层                                                     │
│  - 身份认证 (SSO)                                                    │
│  - 权限检查 (RBAC)                                                   │
│  - 限流 (按用户/部门/系统)                                          │
│  - 路由 (负载均衡)                                                   │
│  - 监控 (APM)                                                        │
│  - 审计日志                                                           │
│  - 🆕 Prompt 注入防护                                                │
│  - 🆕 信任分级 (自动/抽检/全审/禁止)                                 │
│  - 🆕 输入校验 (大小/类型/敏感数据)                                  │
│  - 🆕 资源配额 (4 级: 用户/部门/系统/LLM)                            │
│  来自: 自建 + 第三方网关 (Kong/Envoy)                                │
└───────────────────────────────────┬──────────────────────────────────┘
                                    │
                                    ↓
┌───────────────────────────────────▼────────────────────────────────┐
│  L3 🎯 编排调度层 (大脑)                                              │
│  来自: [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph)│
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  🧠 CEO Agent (决策者)                                          │  │
│  │  - 接收任务 / 全局协调                                           │  │
│  │  - 智能升级处理 (Agent 真搞不定才出手)                         │  │
│  │  - 接收汇报 (不打扰自主解决)                                   │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────┐  ┌────────────────┐  ┌─────────────────────┐  │
│  │ 反思引擎        │  │ 工作流引擎      │  │ 经验管理器           │  │
│  │ (自建)          │  │ (LangGraph)    │  │ (自建)              │  │
│  └────────────────┘  └────────────────┘  └─────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  🧠 LLM Router (自建) - 每 Agent 独立 LLM                      │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  📡 事件总线 (自建)                                              │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  📚 Prompt 治理 (自建) - 集中/版本/审批/A/B                      │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  💸 成本控制 / 🚦 变更管理 / 🧪 测试 QA (自建)                    │  │
│  └────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────┬──────────────────────────────────┘
                                    │
                                    ↓
┌───────────────────────────────────▼────────────────────────────────┐
│  L3.5 通用机制层 (SmartAgentMixin)                                    │
│  来自: 自建 (核心) + [microsoft/autogen](https://github.com/microsoft/autogen)│
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  SmartAgentMixin - 所有 Agent 自动获得:                         │  │
│  │  ┌──────────────────────┐  ┌──────────────────────┐         │  │
│  │  │ ProblemEvaluator     │  │ SmartResolver         │         │  │
│  │  │ 评估问题              │  │ 智能 3 选 1 调度       │         │  │
│  │  └──────────────────────┘  └──────────────────────┘         │  │
│  │  ┌──────────────────────┐  ┌──────────────────────┐         │  │
│  │  │ 经验回流              │  │ 事件上报               │         │  │
│  │  └──────────────────────┘  └──────────────────────┘         │  │
│  │  ┌──────────────────────┐  ┌──────────────────────┐         │  │
│  │  │ MCP 工具自动接入       │  │ 错误分级处理           │         │  │
│  │  └──────────────────────┘  └──────────────────────┘         │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  设计目标: 子 Agent 只实现 do_work(), 其他能力都自动获得              │
└───────────────────────────────────┬──────────────────────────────────┘
                                    │
                                    ↓
┌───────────────────────────────────▼────────────────────────────────┐
│  L4 🤖 Agent 执行层 (薄 Agent + 通用机制)                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │ 📋 产品 Agent │  │ 💻 技术 Agent │  │ 🧪 测试 Agent │               │
│  │ SmartAgent   │  │ SmartAgent   │  │ SmartAgent   │               │
│  │ Mixin ✓      │  │ Mixin ✓      │  │ Mixin ✓      │               │
│  │ do_work:     │  │ do_work:     │  │ do_work:     │               │
│  │ 写 PRD       │  │ 写代码       │  │ 跑测试       │               │
│  └──────────────┘  └──────────────┘  └──────────────┘               │
│  (所有 Agent 几乎一样, 只 do_work 不同)                                │
└───────────────────────────────────┬──────────────────────────────────┘
                                    │
                                    ↓
┌───────────────────────────────────▼────────────────────────────────┐
│  L4.5 🛠️ 能力 / 工具层 - v13 重大升级 ⭐                              │
│  ═══════════════════════════════════                                 │
│                                                                       │
│  1. 插件化工具系统 (借鉴 Obsidian 插件架构)                           │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  工具基类 (Tool ABC) - 统一接口                                │  │
│  │  - name / description / input_schema / execute                 │  │
│  │  - @register 装饰器 - 自动注册                                │  │
│  │  - 加新工具 = 新建一个类 (不碰主代码)                          │  │
│  │  - 热加载 / 独立测试 / 独立版本 / 插件市场                    │  │
│  │  来自: 自建 (借鉴 Obsidian 插件架构)                          │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  2. 🆕 多向链接图 (v13 核心新增, 借鉴 Obsidian 双链)                 │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  MultiLinkGraph - Agent 记忆系统的核心                          │  │
│  │  - 8+ 节点类型 (task/output/failure/experience/tool/...)      │  │
│  │  - 23 种链接类型 (远超 Obsidian 的 2 种)                        │  │
│  │  - 链接带类型/权重/上下文/时间                                  │  │
│  │  - 邻居查询/路径查询/反向查询                                  │  │
│  │  - 存盘到 JSON (Git 友好)                                     │  │
│  │  来自: 自建 (借鉴 Obsidian 双链)                                │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  3. MCP 工具 (标准化)                                                  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  - 第三方 MCP (GitHub/Notion/Jira/Slack)                       │  │
│  │  - 自研产品 MCP (UI/API)                                       │  │
│  │  - 自动发现 / 标准化调用                                       │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  配置文件:                                                           │
│  - config/tools.yaml (工具配置)                                      │
│  - config/mcp_servers.yaml (MCP 配置)                                │
│  - config/graph.yaml (图配置)                                        │
└───────────────────────────────────┬──────────────────────────────────┘
                                    │
                                    ↓
┌───────────────────────────────────▼────────────────────────────────┐
│  L5 💾 数据层 (与 L4.5 集成)                                            │
│  ┌─ 多向链接图存储 ──────────────────────────────────────────┐   │
│  │  - JSON 文件 (Git 友好, Obsidian 风格)                       │   │
│  │  - 节点: data/graph/nodes/{type}/{id}.json                  │   │
│  │  - 链接: data/graph/links/{year}/{month}.jsonl               │   │
│  │  - 索引: 内存中 (启动时加载)                                  │   │
│  └────────────────────────────────────────────────────────────┘   │
│                              +                                      │
│  ┌─ 传统存储 ───────────────────────────────────────────────┐   │
│  │  - Postgres (关系数据)                                     │   │
│  │  - Chroma (向量库)                                        │   │
│  │  - Redis (缓存/限流)                                      │   │
│  │  - S3 (文件)                                              │   │
│  │  - 多级缓存 / 生命周期 / 脱敏 / 并发控制                  │   │
│  └────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────┬──────────────────────────────────┘
                                    │
                                    ↓
┌───────────────────────────────────▼────────────────────────────────┐
│  L6 📊 可观测性层 (OpenTelemetry + Prometheus + Grafana)            │
│  🆕 v13 新增: Dataview 风格的自动查询系统 (借鉴 Obsidian)            │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  - 不用手动维护指标                                            │  │
│  │  - 用查询语句从多向链接图自动算出                              │  │
│  │  - 复杂关系查询 (用图遍历)                                    │  │
│  │  - 实时 (不用缓存)                                           │  │
│  │  - 9 个关键指标自动算                                          │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

### 3.2 智能升级流程

```
[Agent 失败]
       ↓
┌─ ProblemEvaluator ─────────────────────────────────────────────────┐
│  评估 3 维度:                                                        │
│  - 严重度 (LOW/MEDIUM/HIGH/CRITICAL)                                 │
│  - 自己能解决? (查经验 + 评估能力)                                  │
│  - 需要同事? (找有能力的 Agent)                                      │
│  - 信心度 (0-1)                                                      │
└─────────────────────────────────────────────────────────────────────┘
       ↓
3 选 1:
┌─ 路径 A: SELF 自解决 ──────────────────────────────────────────────┐
│  - 重试 do_work() (最多 2 次)                                       │
│  - 成功 → 经验回流                                                   │
│  - 失败 → 升级路径 C                                                │
└─────────────────────────────────────────────────────────────────────┘
┌─ 路径 B: PEER 找同事 (AutoGen) ──────────────────────────────────────┐
│  - 启动 AutoGen GroupChat                                           │
│  - 参与: [本 Agent] + 建议的同事们                                 │
│  - 内部讨论 3-10 轮                                                  │
│  - 解决成功 → 汇报 CEO (不帮) → 经验回流                            │
│  - 解决失败 → 升级路径 C                                            │
└─────────────────────────────────────────────────────────────────────┘
┌─ 路径 C: ESCALATE 升级 CEO ─────────────────────────────────────────┐
│  - 发升级请求事件                                                    │
│  - CEO 决策 (派专家/给资源/改规则/通知人)                           │
│  - Agent 等待 CEO 处理                                               │
│  - 处理完继续执行                                                    │
└─────────────────────────────────────────────────────────────────────┘
目标: 60% 自解决 / 25% 同事讨论 / 15% 升级 CEO
```

---

## 第 4 章 关键子系统详解

### 4.1 SmartAgentMixin 通用机制

```python
class SmartAgentMixin:
    """
    智能 Agent Mixin - 核心抽象
    子类只实现 do_work(), 其他都自动获得
    """
    # 子类必须实现
    agent_name: str
    agent_capabilities: list
    async def do_work(self, task, retry_count=0): ...
    
    # 子类可选重写
    def get_mcp_servers(self) -> list: return []
    
    # 自动获得 (不要改)
    async def execute(self, task):
        # 任务开始 → 干活 → 成功/失败
        # 失败 → 评估 → 3 选 1 → 执行
        # 全程: 事件上报 + 经验回流 + 错误分级
        ...
```

### 4.2 产出物 Schema 标准化

```json
{
  "id": "req-2024-q3-001",
  "type": "requirement",
  "created_at": "2026-06-17T10:30:00Z",
  "created_by": "product_agent",
  "schema_version": "1.0",
  "payload": { /* 业务内容 */ },
  "metadata": { "tags": ["P0"], "priority": "high" },
  "next_steps": [{"action": "tech_estimate", "agent": "tech_agent"}]
}
```

5 个必填字段 = 跨 Agent 兼容的保证。

### 4.3 经验共享机制

```
短期 (Session)     →  当前对话
       ↓ 失败时
失败案例库          →  每次失败 + 根因 + 修复
       ↓ 模式识别
经验库 (Best Practice) → 跨 Agent 通用规则
       ↓ 自动注入
Agent 下次执行     →  加载相关经验
```

### 4.4 错误分级

| 级别 | 例子 | 处理 |
|---|---|---|
| L1 临时 | API 限流、网络 | 重试 3 次 |
| L2 可恢复 | 输入不合规、权限不够 | 引导用户修复 |
| L3 业务 | Agent 能力不足 | 升级 CEO |
| L4 系统 | 数据丢失、安全事件 | 立即告警 + 兜底 |

### 4.5 资源配额

| 级别 | 限制 |
|---|---|
| 用户 | 同时 5 任务 / 每日 $5 / 1M token |
| 部门 | 同时 50 任务 / 每日 $200 / 50M token |
| 系统 | 同时 200 任务 / 每日 $2000 / 500M token |
| LLM API | 100 token/秒 / 1000 待处理队列 |

---

## 第 5 章 智能升级机制详解（4 选 1，决策级别重构）

### 5.1 为什么这个最重要

**核心原则**：**人 > CEO Agent > 子 Agent**

人是最终决策者，CEO Agent 是协调者，子 Agent 是执行者。

### 5.2 4 选 1 升级路径

```
SELF      → 自己重试
PEER      → 找同事讨论（AutoGen 群聊）
HUMAN     → 👤 叫人审批（关键操作直接叫人）
ESCALATE  → 升级 CEO（CEO 协调）
```

### 5.3 关键规则

```
1. 不可逆操作 → 直接叫人, 跳过 CEO
2. 合规/法律相关 → 直接叫人
3. 大影响 (> 100 用户) → 叫人
4. 其余 → 走 AI 内部流程
```

### 5.4 完整决策流程

```
[子 Agent 遇到问题]
       ↓
{判断: 不可逆/合规?}
  ├─ 是 → 👤 直接叫人 (绕过 CEO)
  └─ 否 ↓
[子 Agent 自己能解决?]
  ├─ 是 → SELF (重试)
  └─ 否 ↓
[需要 AI 讨论?]
  ├─ 是 → PEER (AutoGen 群聊)
  └─ 否 ↓
[升级 CEO Agent]
       ↓
[CEO 评估]
  ├─ 能处理 → 处理
  ├─ 派专家 → 派
  └─ 搞不定 → 👤 叫人
       ↓
[👤 人 决策]
  ├─ 批准
  ├─ 拒绝
  └─ 给指导
       ↓
[继续执行]
```

### 5.5 决策权限矩阵

| 操作类型 | 决策权 | 流程 |
|---|---|---|
| 普通任务执行 | 子 Agent | 无需审批 |
| AI 之间讨论 | 子 Agent + CEO | 无需审批 |
| **不可逆操作** | 👤 人 | **直接叫人** (绕过 CEO) |
| **合规/法律** | 👤 人 | **直接叫人** |
| 大影响 | 👤 人 | CEO 整理背景, 叫人决策 |
| 修改配置 | 👤 人 | 叫人 |
| 删除数据 | 👤 人 | 叫人 |
| 部署生产 | 👤 人 | 叫人 |

### 5.5 决策算法

```python
def decide_resolution_path(analysis, action=None):
    # 1. 不可逆 → 直接叫人
    if action and is_irreversible(action):
        return "HUMAN"
    # 2. 合规 → 直接叫人
    if action and is_compliance(action):
        return "HUMAN"
    # 3. AI 解决
    if analysis.can_self_solve and analysis.confidence > 0.8:
        return "SELF"
    elif analysis.needs_peer_help:
        return "PEER"
    # 4. 升级 CEO
    return "ESCALATE"
```

### 5.6 PEER 路径 (AutoGen)

AutoGen GroupChat = 多个 Agent 一起讨论

### 5.7 ESCALATE 路径

CEO Agent 也是协调者。CEO 收到 ESCALATE 后：
1. 自己能处理 → 处理
2. 派专家 → 派
3. 搞不定 → 👤 叫人

### 5.8 目标分布

| 路径 | 比例 |
|---|---|
| SELF (自己重试) | 50% |
| PEER (AI 讨论) | 25% |
| **HUMAN (叫人)** | **15%** |
| ESCALATE (CEO) | 10% |

---

## 第 6 章 MCP 工具集成

### 6.1 MCP 是什么

MCP = 标准化工具接入协议（Anthropic 2024 提出的标准）

类比：USB 协议 → 任何 USB 设备都能插电脑
     MCP 协议 → 任何 MCP 工具都能接 AI Agent

### 6.2 接入的 3 类 MCP

| 类型 | 例子 | 你的 Agent 获得 |
|---|---|---|
| 第三方 MCP | GitHub / Notion / Jira | 查 issue、写文档、查 ticket |
| 自研产品 MCP | your_product_ui / api | 跑自动化测试 |
| 内置工具 | calculator / read_file | 基础操作 |

### 6.3 自研 MCP 的设计原则

**核心原则**：暴露"做什么"，不暴露"怎么做"

```json
// ❌ 错误
{"name": "click", "input_schema": {"selector": "string"}}
// ✅ 正确
{"name": "login", "input_schema": {"username": "string (email)", "password": "string"}}
```

---

## 第 7 章 数据与事件流

### 7.1 数据生命周期

| 数据 | 活跃期 | 归档期 | 删除 |
|---|---|---|---|
| 对话历史 | 7 天 | 30 天 | 30 天后 |
| 失败案例 | 30 天 | 90 天 | 提炼后 |
| 经验库 | 持续 | 持续 | 失效后 |
| 产出物 | 90 天 | 1 年 | 1 年后 |
| 审计日志 | 3 年 | 7 年 | 合规要求 |

### 7.2 事件流

3 类事件：
- `agent.events` — 任务开始/完成/失败
- `agent.escalation` — 升级请求
- `output.validation` — 产出物校验

流向：Agent → 事件总线 → CEO 订阅 → 看板/反思系统/告警系统

---

## 第 8 章 多向链接图系统 (v13 核心新增)

### 8.1 核心思想

**从 Obsidian 双链升级到 N 向链接**：
- 双链 = N=2 的特例（2 个节点，1 条有向边，反向索引）
- 多向链接 = 任意 N 个节点，N 条带类型的边，每个边带权重/上下文/时间

**为什么需要多向**：
- 失败 → 谁引起？根因？类似失败？修复方案？→ 5+ 条边
- 任务 → 谁创建？用了什么工具？产生了什么产出？触发了什么？→ 5+ 条边
- 经验 → 从哪些失败提炼？适用于哪些任务？被谁反驳过？→ 5+ 条边

**N 越大，关系越丰富，查询能力越强**。

### 8.2 节点类型（8+ 种）

| 节点 | 说明 |
|---|---|
| `task` | 任务 |
| `output` | 产出物 |
| `failure` | 失败案例 |
| `experience` | 经验 |
| `tool` | 工具 |
| `user` | 人员 |
| `prompt` | 提示词 |
| `schema` | 模式定义 |
| `decision` | 决策 |
| `feedback` | 用户反馈 |
| `event` | 事件 |

### 8.3 链接类型（23 种，远超 Obsidian 的 2 种）

| 类别 | 链接类型 |
|---|---|
| **内容引用** (2) | `refers_to`, `embeds` |
| **因果关系** (3) | `caused_by`, `causes`, `triggered` |
| **演化关系** (3) | `evolved_from`, `supersedes`, `deprecated_by` |
| **协作关系** (3) | `discussed_with`, `handed_off_to`, `escalated_to` |
| **验证关系** (3) | `validated_by`, `tested_by`, `failed_with` |
| **知识关系** (3) | `references`, `belongs_to`, `part_of` |
| **人员关系** (3) | `created_by`, `modified_by`, `approved_by` |
| **时序关系** (3) | `before`, `after`, `concurrent` |

### 8.4 链接属性

每条链接带 5 个属性：
- `type` - 类型
- `weight` - 权重 (0-1)
- `context` - 上下文 (dict)
- `created_at` - 时间戳
- `created_by` - 谁建的

### 8.5 核心 API

```python
class MultiLinkGraph:
    # 节点操作
    def add_node(node)
    def get_node(id)
    def find_nodes(type, **filters)
    
    # 链接操作
    def link(source, target, type, weight, context, created_by)
    def get_outgoing(node, type)
    def get_incoming(node, type)
    
    # 高级查询
    def neighbors(node, depth)        # N 步邻居
    def path(source, target, max_depth) # 找路径
    def related_with_context(node)    # 相关节点+上下文
```

### 8.6 存盘格式

Git 友好的纯 JSON：
- `data/graph/nodes/{type}/{id}.json`
- `data/graph/links/{year}/{month}.jsonl`

### 8.7 多向链接驱动 Agent 决策

**任务失败时**：
1. 创建失败节点 + 5+ 条链接
2. 查询"类似失败怎么解决"（图遍历）
3. 找到修复经验 → 自己修
4. 没找到 → 升级 CEO

**接到新任务时**：
1. 查"类似任务的历史"（3 步邻居）
2. 查"相关经验"（APPLIES_TO 链接）
3. 查"要避开的失败"（REMINDED_BY 链接）
4. 用这些上下文做决策

---

## 第 9 章 插件化工具系统 (v13 新增)

### 9.1 借鉴 Obsidian 插件架构

Obsidian 的插件设计精髓：
- 统一基类（`Plugin` ABC）
- 完整接口（`onload/onunload/...`）
- 自动发现 + 加载
- 热加载（不重启）
- 独立版本

### 9.2 v13 改造

之前（v12）：
```python
# agent.py - 工具散落
def run_tool(name, inputs):
    if name == "calculator": ...
    elif name == "read_file": ...
    # 100 行 if-elif
```

现在（v13）：
```python
# tool_base.py - 插件基类
class Tool(ABC):
    name: str
    description: str
    input_schema: dict
    @abstractmethod
    def execute(self, inputs): ...

@register
class CalculatorTool(Tool):
    name = "calculator"
    def execute(self, inputs):
        return str(eval(inputs["expression"]))

# tools/ 目录 - 每个工具一个文件
# tools/calculator.py
# tools/read_file.py
# 加新工具 = 新建一个文件，不碰主代码
```

### 9.3 关键收益

- 加新工具 = 写一个类（不碰主代码）
- 工具独立测试
- 工具独立发布
- 自动注册 + 自动发现
- 热加载（无需重启）

### 9.4 配置文件

```yaml
# config/tools.yaml
tools:
  enabled:
    - calculator
    - read_file
    - web_search
  config:
    read_file:
      max_size: 10000
    web_search:
      engine: duckduckgo
      max_results: 5
```

---

## 第 10 章 Dataview 风格查询 (v13 新增)

### 10.1 借鉴 Obsidian Dataview

Obsidian Dataview：在 .md 里写查询语句，自动算出结果。

```dataview
TABLE priority, status
FROM #project
WHERE status = "进行中"
SORT priority DESC
```

### 10.2 v13 改造

用类似语法从多向链接图自动查询：

```python
# 看板自动算
query("""
    SELECT task, status, agent, started_at
    FROM tasks
    WHERE status = 'running'
    ORDER BY started_at DESC
""")

# Agent 决策
query("""
    SELECT AVG(success_rate)
    FROM experiences
    WHERE evolved_from IN (
        SELECT failure FROM failures
        WHERE task_id = current_task
    )
""")

# 影响分析
query("""
    SELECT task, status
    FROM tasks
    WHERE 2 STEPS FROM current_change
""")
```

### 10.3 关键收益

- 不用手动维护指标（自动算）
- 复杂关系查询（图遍历）
- 实时（不用缓存）
- 可视化友好

### 10.4 9 个自动算的指标

| 指标 | 怎么算 |
|---|---|
| 端到端成功率 | COUNT(passed) / COUNT(total) |
| 平均完成时间 | AVG(duration) |
| 成本/任务 | SUM(cost) / COUNT(tasks) |
| 用户满意度 | AVG(feedback.score) |
| 失败率 (按 Agent) | COUNT(failures) / COUNT(tasks) GROUP BY agent |
| 反思触发率 | COUNT(reflections) / COUNT(failures) |
| 升级请求率 | COUNT(escalations) / COUNT(failures) |
| 校验失败率 | COUNT(validation_fails) / COUNT(outputs) |
| 经验有效性 | AVG(success_rate) GROUP BY experience |

---

## 第 11 章 上下文隔离

### 11.1 6 种空间

```
┌─ 私有空间 (Private) ──────── 只能自己看
┌─ 权限组空间 (Perm Group) ─── 同权限组成员共享
┌─ 群组空间 (Group) ────────── 群组成员共享
┌─ 项目空间 (Project) ──────── 跨群组临时项目
┌─ 客户/外部空间 (External) ── 跟客户/供应商共享
┌─ 全租户可见 (Tenant Public) ─ 整个租户可见
```

### 11.2 访问控制

```python
class AccessControl:
    def can_read(self, user, resource) -> bool:
        # 1. 租户隔离 (硬性)
        if resource.tenant_id != user.tenant_id:
            return False
        # 2. 平台管理员
        if user.global_role == "platform_admin":
            return True
        # 3. 所有者
        if resource.owner_id == user.user_id:
            return True
        # 4. 公开
        if resource.visibility == "tenant_public":
            return True
        # 5. 显式共享
        if user.user_id in resource.shared_with:
            return True
        # 6. 按可见性级别
        if resource.visibility in ("perm_group", "group", "project", "external"):
            return self._check_scope(user, resource)
        return False
```

---

## 第 12 章 失败 UX

### 12.1 5 个阶段

```
阶段 1: 等待 (进度条 + 实时日志 + 可取消)
阶段 2: 失败 (友好错误 + 分类 + 建议)
阶段 3: 重试 (一键重试 + 换参数 + 换 Agent)
阶段 4: 记录 (自动入反思库)
阶段 5: 通知 (闭环)
```

### 12.2 半成品保留

```python
class TaskCheckpoint:
    """每完成一步就保存，失败时能继续"""
    task_id: str
    completed_steps: list
    pending_steps: list
    intermediate_outputs: list
    error_history: list
    can_resume: bool
```

---

## 第 13 章 时间处理

### 13.1 核心原则

```
1. 存 UTC，显示本地
2. 跨时区计算
3. 工作时间不打扰（紧急除外）
4. 节假日不打扰
```

### 13.2 超时策略

| 任务类型 | 超时 |
|---|---|
| quick | 1 分钟 |
| standard | 5 分钟 |
| complex | 30 分钟 |
| long | 2 小时 |
| batch | 1 天 |

---

## 第 14 章 协作冲突

### 14.1 4 类冲突

```
冲突 1: 抢资源 (锁)
冲突 2: 锁等待 (心跳)
冲突 3: 内容冲突 (乐观锁 + 3-way merge)
冲突 4: 优先级 (插队策略)
```

### 14.2 锁机制

```python
class ResourceLock:
    def acquire(self, resource, holder, ttl=300) -> bool:
        """Redis 分布式锁"""
        return self.redis.set(f"lock:{resource}", holder, nx=True, ex=ttl)
    
    def release(self, resource, holder):
        current = self.redis.get(f"lock:{resource}")
        if current == holder:
            self.redis.delete(f"lock:{resource}")
    
    def heartbeat(self, resource, holder):
        """长任务续期"""
        self.redis.expire(f"lock:{resource}", 300)
```

### 14.3 冲突解决

```
- 简单冲突: 3-way merge (自动合并)
- 数据冲突: Last Write Wins
- 无法自动解决: 标出来让人处理
```

---

## 第 15 章 技术栈

| 类别 | 技术 | 来源 |
|---|---|---|
| 基础 | Python 3.10+ | 开源 |
| Agent 框架 | **LangGraph** | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) |
| 内部协作 | **AutoGen** | [microsoft/autogen](https://github.com/microsoft/autogen) |
| 记忆系统 | **多向链接图 (自建)** | 借鉴 Obsidian 双链 |
| 工具系统 | **插件化 (自建)** | 借鉴 Obsidian 插件 |
| LLM | Claude Sonnet / Haiku | Anthropic API |
| 关系数据 | PostgreSQL | 开源 |
| 向量库 | Chroma | [chroma-core/chroma](https://github.com/chroma-core/chroma) |
| 缓存 | Redis | 开源 |
| 文件 | S3 / MinIO | 开源 |
| 事件 | Kafka (可选) | Apache |
| 图算法 | NetworkX (备选) | [networkx.org](https://networkx.org/) |
| 链路追踪 | OpenTelemetry | CNCF |
| 指标 | Prometheus | CNCF |
| 看板 | Grafana | 开源 |
| 日志 | ELK | 开源 |
| 容器 | Docker / K8s | 开源 |
| 工具协议 | MCP | [modelcontextprotocol.io](https://modelcontextprotocol.io/) |

### 11.1 自建模块清单

| 模块 | 作用 |
|---|---|
| SmartAgentMixin | 通用机制（升级/经验/错误） |
| ProblemEvaluator | 问题评估 |
| SmartResolver | 智能 3 选 1 调度 |
| AutoGenFactory | AutoGen 团队工厂 |
| LLM Router | 每 Agent 独立 LLM |
| 经验管理器 | 3 层记忆 + 抽象 |
| Schema 验证门 | 产出物校验 |
| 事件总线 | Agent 间通信 |
| 资源配额 | 4 级配额管理 |
| MCP 客户端 | 工具接入 |
| 反思引擎 | 失败分析 |
| 看板 | 可视化 |
| Prompt 治理 | 版本/审批/A/B |
| **MultiLinkGraph** (v13) | **多向链接图** |
| **Tool ABC** (v13) | **插件化工具基类** |
| **Dataview 引擎** (v13) | **自动查询系统** |

---

## 第 16 章 实施路径

### 12.1 6 个月路线图

#### Month 1：基础 + 1 个 Agent
- [ ] 搭 LangGraph 主图
- [ ] 写 SmartAgentMixin 骨架
- [ ] 写产品 Agent (用自研 MCP)
- [ ] 接入 2-3 个 MCP server
- [ ] 跑通 5 个真实需求

#### Month 2：扩到 4 个 Agent
- [ ] + 技术 Agent
- [ ] + 测试 Agent
- [ ] + 反思 Agent
- [ ] 实现 PEER 路径 (AutoGen)
- [ ] 实现经验共享
- [ ] 跑通 20 个真实需求

#### Month 3：智能升级完善
- [ ] 完善 SmartResolver
- [ ] 接入 Notion / Jira MCP
- [ ] 看板 + 告警
- [ ] 产出物 Schema 完整
- [ ] 跑通 50 个真实需求

#### Month 4：自研 MCP + 自动化测试
- [ ] 设计自研产品 MCP (UI + API)
- [ ] 接入到测试 Agent
- [ ] CI/CD 集成
- [ ] Prompt 治理 (版本/审批)
- [ ] 资源配额

#### Month 5：v13 升级
- [ ] 实现 MultiLinkGraph
- [ ] 工具插件化
- [ ] Dataview 查询引擎
- [ ] 改造 agent.py 接入新机制

#### Month 6：生产化
- [ ] 监控 + 告警
- [ ] 错误处理 4 级
- [ ] 数据生命周期
- [ ] 安全防护 4 层
- [ ] 性能优化

### 12.2 关键里程碑

| 里程碑 | 时间 | 验证标准 |
|---|---|---|
| M1: 跑通 1 流程 | 第 1 月末 | 5 个真实需求成功 |
| M2: 4 Agent 协作 | 第 2 月末 | 端到端 20 个需求 |
| M3: 智能升级 | 第 3 月末 | 60% 自解决、25% 同事、15% CEO |
| M4: 自研 MCP 测试 | 第 4 月末 | 自动化测试在 CI 跑通 |
| M5: v13 升级 | 第 5 月末 | 多向链接 + 插件化 + Dataview |
| M6: v15.1 升级 | 第 6 月末 | 决策级别重构 + 上下文隔离 + 失败 UX + 时间处理 + 协作冲突 |
| M7: 上线 | 第 6-7 月末 | 业务部门能用 |

---

## 第 17 章 风险与对策

| 风险 | 严重度 | 对策 |
|---|---|---|
| LLM API 涨价 | 中 | 多厂商支持、自动降级 |
| 核心人员离职 | 高 | 文档完善、关键角色备份 |
| Agent 幻觉扩散 | 高 | 产出物校验门 + 关键步骤人审 |
| 成本失控 | 中 | 4 级配额 + 异常告警 |
| 数据泄露 | 极高 | 脱敏 + 权限分级 + 审计 |
| Prompt 改坏 | 中 | 版本管理 + 灰度发布 + A/B |
| 系统单点故障 | 高 | 多副本 + 降级方案 + 监控 |
| 经验库污染 | 中 | 经验评估 + 定期 review |
| 多向链接图膨胀 | 中 | 定期清理 + 索引优化 |
| 工具插件化破坏 | 低 | 版本管理 + 向后兼容 |
| 上下文隔离不严 | 高 | 多层校验 + 审计 |
| 锁死锁 | 中 | 超时 + 心跳 + 自动释放 |
| v15.1 决策逻辑复杂 | 中 | 单元测试 + 模拟演练 |

---

## 附录 A 项目来源

| 项目 | URL | 用途 |
|---|---|---|
| LangGraph | https://github.com/langchain-ai/langgraph | 主流程框架 |
| AutoGen | https://github.com/microsoft/autogen | 内部多 Agent 协作 |
| Model Context Protocol | https://modelcontextprotocol.io/ | 工具协议 |
| MCP Servers (官方) | https://github.com/modelcontextprotocol/servers | 各种 MCP server |
| MCP Python SDK | https://github.com/modelcontextprotocol/python-sdk | 自己写 MCP 用 |
| Obsidian | https://github.com/obsidianmd/obsidian-releases | 借鉴: 插件 + 双链 + Dataview |
| Obsidian Dataview | https://github.com/blacksmithgu/obsidian-dataview | 借鉴: 查询系统 |
| Chroma | https://github.com/chroma-core/chroma | 向量数据库 |
| NetworkX | https://networkx.org/ | 图算法 (备选) |
| Anthropic SDK | https://github.com/anthropics/anthropic-sdk-python | Claude API |
| OpenTelemetry | https://opentelemetry.io/ | 链路追踪 |
| Prometheus | https://prometheus.io/ | 指标 |
| Grafana | https://grafana.com/ | 看板 |

---

## 附录 B 术语表

| 术语 | 含义 |
|---|---|
| Agent | AI 助手，能调 LLM 和工具 |
| MCP | Model Context Protocol，标准化工具协议 |
| Tool | Agent 能调用的函数（v13 插件化） |
| Schema | 产出物结构定义 |
| Mixin | 代码复用模式，把通用逻辑"混入"多个类 |
| ReAct | Reason + Act，Agent 思考-行动循环 |
| RAG | Retrieval-Augmented Generation，检索增强生成 |
| PEER | 智能升级路径 B：找同事 |
| SELF | 智能升级路径 A：自己解决 |
| ESCALATE | 智能升级路径 C：升级 CEO |
| Vault | Obsidian 笔记库的文件夹 |
| Frontmatter | Markdown 文件头部的 YAML 元数据 |
| MOC | Map of Content，Obsidian 索引页 |
| **MultiLinkGraph** | **多向链接图 (v13 新增)** |
| **Plugin Tool** | **插件化工具 (v13 新增)** |
| **Dataview** | **自动查询 (v13 新增)** |

---

## 附录 C 借鉴 Obsidian 的设计

v13 借鉴 Obsidian 3 大设计：

| Obsidian 设计 | 借鉴到 Agent 系统 | 价值 |
|---|---|---|
| **双链 [[]]** | **多向链接图** | 记忆系统的 10 倍升级 |
| **插件系统** | **插件化工具** | 加新工具零成本 |
| **Dataview** | **自动查询引擎** | 不用手维护指标 |

### C.1 关键洞察

1. **文件 = 纯文本**：Obsidian 全部 .md 文件 → 你的 Agent 全部 JSON 文件（Git 友好）

2. **双链 = N 向的特例**：
   - Obsidian 只有 2 种链接（refers_to, embeds）
   - 你的 Agent 有 23 种链接（内容/因果/演化/协作/验证/知识/人员/时序）

3. **插件统一基类**：
   - Obsidian 插件继承 `Plugin` ABC
   - 你的工具继承 `Tool` ABC，加新工具零成本

4. **Dataview 自动查询**：
   - Obsidian 用 `TABLE ... FROM #tag` 自动算
   - 你的 Agent 用类似语法从多向链接图自动算

5. **本地优先 + Git 友好**：
   - Obsidian = 文件夹 = `git init`
   - 你的 Agent 系统 = 同样的设计

### C.2 借鉴但不照搬

**保留 Obsidian**：
- 纯文本/JSON（Git 友好）
- 插件架构（独立测试、热加载）
- 链接 + 反向链接（多向升级）
- 本地优先

**超越 Obsidian**：
- N 向链接（23 种 vs 2 种）
- 链接带权重/上下文/时间
- 图遍历查询（不仅是反向）
- Dataview 风格自动算指标
- 链接驱动 Agent 决策

---

## 总结

**5 个核心创新**：

1. **抽象的通用机制**（SmartAgentMixin）—— 每个 Agent 自动有智能能力
2. **智能升级**（SELF/PEER/ESCALATE）—— 解决多 Agent 协作的关键
3. **MCP 标准化** —— 工具接入零成本
4. **多向链接图** —— 超越双链的记忆系统
5. **插件化工具系统** —— 借鉴 Obsidian 插件架构

**3 个借鉴** (v13)：
- 多向链接图（自 Obsidian 双链）
- 插件化工具（自 Obsidian 插件）
- Dataview 查询（自 Obsidian Dataview 插件）

**项目来源**：
- LangGraph (主图) + AutoGen (内) + MCP (工具)
- Obsidian (插件架构 + 双链 + Dataview) ⭐ v13
- 自建（核心）

**v13 相对 v12 升级**：
- 记忆系统：单线 → 多向
- 工具系统：散落 → 插件化
- 指标系统：手动 → 自动

**Git 友好 + 本地优先**：整个 Agent 系统可 `git init` 管理，纯 JSON/纯文本
