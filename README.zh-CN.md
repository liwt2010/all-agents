# Agent System

[![CI](https://github.com/agent-system/agent-system/actions/workflows/ci.yml/badge.svg)](https://github.com/agent-system/agent-system/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**企业级多 Agent 协作平台** — 一个由 AI 驱动的工作流系统，内置 9 个 Agent、插件化工具、多租户隔离和生产级安全能力。

```
用户 → CEO Agent → 产品 Agent → 技术 Agent → 测试 Agent → 部署 Agent → DevOps Agent
                 ↘ 安全 Agent    ↘ 文档 Agent   ↘ 审查 Agent
```

## 为什么用 Agent System？

| 跟单个 AI 聊天... | 你得到一个 **AI 团队** |
|---|---|
| 单次回答 | 多步流水线 + 同行评审 |
| 单次上下文窗口 | 共享记忆（MultiLinkGraph） |
| 手动切换工具 | 自动化 MCP 工具发现 |
| 无审计痕迹 | 完整审计日志 + LLM 成本追踪 |

## 快速开始

```bash
# 安装
pip install -e ".[api]"

# 设置 API key（Anthropic、兼容 OpenAI 或本地模型）
export ANTHROPIC_API_KEY=sk-xxx

# 运行单个 Agent
python -m agent_system run "写一个登录功能的 PRD"

# 运行完整流水线（产品 → 技术 → 测试）
python -m agent_system pipeline "开发一个待办事项应用"

# 启动 API 服务
uvicorn agent_system.api.server:app --port 8000
```

## Agent 清单

| Agent | 职责 | 核心能力 |
|-------|------|----------|
| **CEO** | 总调度 | 任务分配、升级处理、流水线管理 |
| **产品** | 需求 | PRD 编写、功能拆解、验收标准 |
| **技术** | 实现 | 代码生成、架构设计、代码审查 |
| **测试** | 质量 | 测试生成、执行、覆盖率分析 |
| **部署** | 运维 | 预发/生产发布、迁移执行、回滚 |
| **DevOps** | 基础设施 | CI/CD、K8s、监控、IaC 审查 |
| **安全** | 合规 | 密钥扫描、依赖 CVE、威胁建模 |
| **文档** | 文档 | API 参考、运行手册、ADR、更新日志 |
| **审查** | 同行评审 | 代码/设计/测试计划审查、合并审批 |

## 功能特性

- **9 个内置 Agent** 覆盖完整产品生命周期
- **智能升级** — SELF / PEER / HUMAN / ESCALATE（4 路决策）
- **多向链接图** — 11 种节点类型、23 种链接类型、时间衰减经验记忆
- **插件化工具系统** — `@register` 装饰器、自动发现、热加载
- **MCP 协议** — 连接任意 MCP 兼容工具服务器
- **多租户** — 6 级空间隔离模型（私有 → 租户公开）
- **RBAC** — 6 种角色、7 种权限、权限组覆盖
- **9 个自动计算指标** — 从记忆图实时输出
- **实时进度** — WebSocket + REST进度轮询 + 断点续传
- **安全** — 输入校验、密钥检测、限流、文件沙箱
- **分布式锁** — Redis 后端 + 内存降级

## API 端点

| 端点 | 方法 | 认证 | 描述 |
|------|------|------|------|
| `/api/health` | GET | 无 | 存活检查 |
| `/api/ready` | GET | 无 | 就绪检查（验证 DB、LLM） |
| `/api/auth/token` | POST | 无 | 颁发 JWT（仅开发环境） |
| `/api/agents` | GET | JWT | 列出可用 Agent |
| `/api/tasks` | POST | JWT | 提交任务 |
| `/api/tasks/{id}` | GET | JWT | 获取任务结果 |
| `/api/tasks` | GET | JWT | 任务列表（分页、租户隔离） |
| `/api/tasks/{id}/progress` | GET | JWT | 实时进度 |
| `/api/ws/{id}` | WS | JWT | WebSocket 状态流 |
| `/api/graph/stats` | GET | JWT | 图统计 |
| `/api/metrics` | GET | JWT | Prometheus 指标 |

## 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ANTHROPIC_API_KEY` | — | LLM API 密钥（生产环境必填） |
| `AUTH_SECRET` | — | JWT 签名密钥（至少 32 字符） |
| `ENVIRONMENT` | `development` | `production` 启用严格模式 |
| `POSTGRES_URL` | — | Postgres 连接字符串 |
| `REDIS_URL` | — | Redis 连接字符串 |
| `RATE_LIMIT_PER_MINUTE` | 60 | 每 IP 每分钟请求数 |
| `ALLOWED_FILE_ROOTS` | `data,tmp,.` | 逗号分隔的文件沙箱路径 |
| `CORS_DEV_ORIGINS` | — | 额外 CORS 源（开发用） |

## 生产部署

```bash
# Docker
docker-compose up --build

# Helm (K8s)
helm install agent-system ./deploy/helm \
  --set env.anthropicApiKey=$ANTHROPIC_API_KEY \
  --set env.postgresUrl=$POSTGRES_URL
```

运维手册请参见 [docs/RUNBOOK.md](docs/RUNBOOK.md)。

## 界面预览

```
Dashboard:  [9 个指标卡片] [Agent 列表] [最近任务]
Submit:     [文本输入] [Agent 选择] → [实时进度条] [结果 JSON]
Tasks:      [按状态筛选] [分页列表]
Graph:      [节点/链接图表] [年龄分布]
Metrics:    [Sparkline 图块] [每 3 秒自动刷新]
```

## 项目结构

```
src/agent_system/
├── agents/       # 9 个内置 Agent
├── api/          # FastAPI 服务
├── core/         # SmartAgent、LLM 路由、认证、RBAC、事件、缓存
├── memory/       # 多向链接图、经验回流
├── tools/        # 插件化工具系统 + MCP 客户端
├── storage/      # Postgres + Redis 后端
├── concurrency/  # 分布式锁
├── migration/    # 数据迁移引擎
├── observability/ # 追踪 + Prometheus 指标
├── config/       # 配置管理器（4 层覆盖）
├── auth/         # JWT + RBAC
└── onboarding/   # 首次用户体验
```

## 性能基准

| 操作 | p50 | p95 | 样本数 |
|------|-----|-----|--------|
| 健康检查 | 6ms | 15ms | 200 |
| 图节点查询 | 0.4μs | 0.5μs | 1000 |
| 限流检查 | 1.3μs | 2.2μs | 10000 |
| 审计日志写入 | 4μs | 8μs | 1000 |

## 路线图

- [ ] WebSocket 流式 LLM 响应
- [ ] 自定义 Agent 模板市场
- [ ] AutoGen 原生同行讨论
- [ ] OpenTelemetry SDK 导出
- [ ] Grafana 看板 JSON
- [ ] GitHub App 集成（自动 PR 审查）

## 许可证

MIT
