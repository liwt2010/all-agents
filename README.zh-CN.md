# Agent System · 多智能体协作平台

[![CI](https://github.com/liwt2010/all-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/liwt2010/all-agents/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![v0.3.0](https://img.shields.io/badge/release-v0.3.0-blue)](https://github.com/liwt2010/all-agents/releases/tag/v0.3.0)

> **企业级多智能体编排平台** — 生产级 AI 智能体系统,具备共享记忆、Schema 宽容、数据溯源、分布式追踪、OpenAPI/SDK 自动生成、端到端可观测性。

```
User → CEO Agent → Product Agent → Tech Agent → Test Agent → Deploy Agent
                    ↘ Security    ↘ Docs      ↘ Review       ↘ DevOps
```

📖 **其他语言**: [English](README.md) · [繁體中文](README.zh-TW.md)

---

## 为什么选择 Agent System?

| 单个 AI | Agent System |
|---|---|
| 一次性回答 | 多步流水线 + 同行评审 |
| 单一上下文窗口 | 共享记忆图谱(11 种节点类型、23 种链接类型) |
| 手动切换工具 | 自动发现 MCP 工具注册表 |
| 没有审计追踪 | 完整审计日志 + LLM 成本追踪 |
| 静默失败 | 数据溯源标签(REAL_LLM / MOCK / LLM_FAILURE) |
| 运维不透明 | Prometheus + OpenTelemetry 开箱即用 |
| 难以扩展 | 自定义 Agent 平台 + OpenAPI/SDK 自动生成 |

---

## 快速开始

```bash
# 1. 安装
pip install -e ".[api,storage]"

# 2. 配置
export ANTHROPIC_API_KEY=sk-xxx           # 或 OPENAI_API_KEY
export AUTH_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")

# 3. 运行单个 Agent
python -m agent_system run "写一个登录功能的 PRD"

# 4. 运行完整流水线
python -m agent_system pipeline "构建一个 todo 应用"

# 5. 启动 API 服务
uvicorn agent_system.api.server:app --host 0.0.0.0 --port 8000
```

或者使用 Docker:

```bash
docker run -d --name agent-system \
  -e AUTH_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))") \
  -e ANTHROPIC_API_KEY=sk-xxx \
  -p 8000:8000 \
  liwt2010/all-agents:v0.1.0
```

访问 API:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- OpenAPI JSON: http://localhost:8000/openapi.json
- 健康检查: http://localhost:8000/api/health
- 指标: http://localhost:8000/metrics

---

## 智能体列表

| Agent | 角色 | 核心能力 |
|-------|------|----------|
| **CEO** | 编排者 | 任务分发、4 路径升级、流水线管理 |
| **Product** | 需求 | PRD 编写、功能拆解、验收标准 |
| **Tech** | 实现 | 代码生成、架构设计、代码审查 |
| **Test** | 质量 | 测试生成、执行、覆盖率分析 |
| **Deploy** | 运维 | 预发/生产发布、迁移执行、回滚 |
| **Security** | 合规 | 密钥扫描、CVE、威胁建模 |
| **Docs** | 文档 | API 参考、运维手册、ADR、变更日志 |
| **Review** | 同行评审 | 代码/设计/测试方案评审、合并批准 |
| **DevOps** | 基础设施 | CI/CD、K8s、监控、IaC 审查 |

---

## 生产级特性 (v0.1.0)

### 核心平台
- **9 个内置智能体**(5 个生产 + 4 个专项)
- **SmartAgent.execute()** 拆分为 checkpoint / retry / failure / escalate
- **Dataview 引擎** — 在记忆图谱上跑类 SQL 查询
- **4 路径解析器**:SELF / PEER / HUMAN / ESCALATE
- **AgentRegistry** 动态查找智能体
- **自定义 Agent 平台** — Pydantic v2 友好,支持热重载

### 记忆与学习
- **MultiLinkGraph** — 11 种节点类型、23 种链接类型、时间衰减相似度
- **经验反馈循环** — 失败的任务为后续尝试提供参考
- **`memory_enabled` 可选关闭** — 支持临时性工作流

### Schema 与数据完整性
- **4 级 Schema 宽容**(STRICT / LENIENT / REPAIR / WARN),支持自动修复
- **数据溯源** 每次输出: `REAL_LLM`(置信度 0.85)/ `MOCK`(0.0)/ `LLM_FAILURE`(0.0)
- **FailureNodeLogger** — 每次 LLM 失败都成为可审计的图谱节点
- **`raw_output` 兜底** — 部分结果绝不静默失败

### 可观测性
- **OpenTelemetry 分布式追踪** — DISABLED / CONSOLE / OTLP_HTTP 三种模式
  - `agent.execute` span 包含状态 + 异常
  - FastAPI 中间件自动包装每个 HTTP 请求
- **Prometheus 指标** — 11 个指标位于 `/metrics`
- **批量审计日志** — 支持保留期(默认 90 天)+ HTTP 查询接口
- **请求 ID 透传** 通过 `X-Request-ID` 头

### API 与 SDK
- **OpenAPI 3.1** 规范,元数据丰富(3 个 server、7 个 tag、9 个 schema)
- **Python SDK** 通过 `openapi-python-client` 自动生成
- **TypeScript SDK** 通过 `openapi-typescript-codegen` 自动生成
- **`make codegen`** 一键重新生成

### 安全加固
- **CORS** — 环境感知,生产环境拒绝 `*`,强制 `https://`
- **TLS** — HSTS 头(生产环境默认开启)、HTTPS 重定向中间件、安全 cookie 检查
- **JWT 密钥轮换** — `AUTH_SECRETS="kid:secret,..."` 多密钥,零停机滚动
- **滑动窗口限流** — 按用户 + 按作用域
- **请求体大小限制**(默认 1MB)+ **请求中密钥检测**
- **输入清洗** — Prompt 注入检测(TrustLevel 感知)

### 存储与运维
- **可插拔存储** — JSON / SQLite / PostgreSQL
- **备份子系统** — cron + SHA-256 清单 + tar.gz + DR 演练
- **分布式锁** — Redis 后端,内存兜底
- **迁移 CLI** — 切换后端不丢数据
- **多租户隔离** — 6 空间隔离模型
- **RBAC** — 6 个角色、7 个权限、权限组覆盖

### 开发者体验
- **生产部署指南** — [docs/PRODUCTION.md](docs/PRODUCTION.md)(11KB,15 章节)
- **故障响应手册** — [docs/RUNBOOK.md](docs/RUNBOOK.md)
- **发布说明** — [RELEASE_NOTES.md](RELEASE_NOTES.md)
- **CI 门禁** — 生产就绪测试套件阻止低质量 PR

---

## API 端点

| 端点 | 方法 | 鉴权 | 说明 |
|----------|--------|------|------|
| `/api/health` | GET | 否 | 存活探针 |
| `/api/ready` | GET | 否 | 就绪探针(检查 DB、LLM) |
| `/api/auth/token` | POST | 否 | 签发 JWT(仅开发环境 — v0.2.0 改 RS256) |
| `/api/agents` | GET | JWT | 列出可用智能体 |
| `/api/tasks` | POST | JWT | 提交任务 |
| `/api/tasks/{id}` | GET | JWT | 获取任务结果 |
| `/api/tasks` | GET | JWT | 列出任务(分页、按租户隔离) |
| `/api/tasks/{id}/progress` | GET | JWT | 实时进度 |
| `/api/graph/stats` | GET | JWT | 图谱统计 |
| `/api/graph/node/{id}` | GET | JWT | 获取指定图谱节点 |
| `/api/audit/query` | GET | JWT | 查询审计日志 |
| `/api/metrics` | GET | JWT | 应用指标(JSON) |
| `/metrics` | GET | 否 | Prometheus 抓取端点 |

完整 OpenAPI 规范: [/openapi.json](http://localhost:8000/openapi.json)

---

## 配置

| 环境变量 | 必填 | 默认值 | 说明 |
|---------|----------|---------|------|
| `ANTHROPIC_API_KEY` | 是* | — | LLM API 密钥(Anthropic / OpenAI 兼容) |
| `AUTH_SECRET` | 是 | — | JWT 签名密钥(32+ 字符)— 或使用 `AUTH_SECRETS` 进行轮换 |
| `ENVIRONMENT` | 否 | `development` | 设为 `production` 启用严格模式 |
| `LLM_PROVIDER` | 否 | `anthropic` | `anthropic` / `openai` / `mock` |
| `LLM_MODEL` | 否 | (provider 默认) | 模型名(如 `claude-3-5-sonnet`) |
| `CORS_ALLOWED_ORIGINS` | 生产必填 | localhost:5173(开发) | 逗号分隔的 https:// 来源 |
| `AGENT_OTEL_ENABLED` | 否 | `false` | 启用 OpenTelemetry 追踪 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 启用 OTEL 时 | `http://localhost:4318` | OTLP/HTTP collector 地址 |
| `STORAGE_BACKEND` | 否 | `json` | `json` / `sqlite` / `postgres` |
| `POSTGRES_URL` | postgres 时 | — | PostgreSQL 连接字符串 |
| `REDIS_URL` | 否 | — | Redis 地址(用于分布式锁) |
| `RATE_LIMIT_PER_MINUTE` | 否 | 120 | 每用户每分钟请求数(默认作用域) |
| `AGENT_RATE_LIMIT_ENABLED` | 否 | `true` | 开关滑动窗口限流 |
| `AGENT_AUDIT_RETENTION_DAYS` | 否 | 90 | 审计日志保留期 |
| `AGENT_BACKUP_CRON` | 否 | `0 2 * * *` | 备份计划(cron 格式) |
| `TLS_REDIRECT_ENABLED` | 否 | `false` | 启用 HTTP→HTTPS 301 |
| `TLS_HSTS_ENABLED` | 否 | `true`(生产) | 添加 HSTS 头 |
| `MAX_REQUEST_BYTES` | 否 | `1048576` | 请求体上限(1MB) |
| `ALLOWED_FILE_ROOTS` | 否 | `data,tmp` | 文件沙箱根目录 |

*开发环境使用 `mock` provider 时无需密钥。

完整注释列表见 [.env.example](.env.example)。

---

## 项目结构

```
src/agent_system/
├── agents/          # 9 个内置智能体(Product、Tech、Test、Deploy、CEO、Security、Docs、Review、DevOps)
├── api/             # FastAPI 服务(OpenAPI、中间件、WebSocket)
├── auth/            # JWT + RBAC + 多租户上下文(从 core/auth/ 重新导出)
├── codegen/         # OpenAPI 规范导出 + Python/TypeScript SDK 生成器(PR-15)
├── concurrency/     # 分布式锁(Redis + 内存兜底)
├── config/          # ConfigManager(4 层配置覆盖)
├── core/            # SmartAgent、LLM 路由、安全中间件、审计
│   ├── auth/        # JWT、RBAC、TenantContext
│   ├── observability/  # DataProvenance、tracing
│   ├── rate_limit/  # 滑动窗口限流器 + 注册表(PR-12)
│   ├── backup/      # 清单 + 调度器 + 还原 + 保留(PR-13)
│   ├── security/    # CORS + TLS + 密钥轮换(PR-16)
│   └── ...
├── memory/          # MultiLinkGraph、经验反馈循环、embeddings
├── observability/   # Prometheus 指标、OTel 导出器 + 中间件(PR-14)
├── storage/         # JSON / SQLite / PostgreSQL 后端 + 迁移 CLI
├── tools/           # 插件工具系统 + MCP 客户端
├── migration/       # 数据迁移引擎
└── onboarding/      # 首次用户体验
```

---

## 使用 SDK

### Python

```python
from agent_system_api_client import Client
from agent_system_api_client.api.default import health_api_health_get

client = Client(base_url="https://api.example.com")
response = health_api_health_get.sync(client=client)
print(response)
```

### TypeScript

```typescript
import { Configuration, DefaultApi } from 'agent-system-client';

const config = new Configuration({ basePath: 'https://api.example.com' });
const api = new DefaultApi(config);
const health = await api.healthApiHealthGet();
console.log(health);
```

### 重新生成 SDK

```bash
make codegen      # OpenAPI 规范 + Python SDK
make codegen-ts   # 加上 TypeScript SDK(需要 Node.js)
```

---

## 测试

```bash
# 单元测试(始终运行,无需 LLM)
pytest tests/ -q --ignore=tests/test_*real_llm.py

# 真实 LLM 端到端测试(需要 API 密钥)
ANTHROPIC_API_KEY=sk-xxx pytest tests/test_*real_llm.py -v

# 生产就绪门禁(CI 始终运行)
pytest tests/test_production_readiness.py -v
```

**当前状态**:362 个单元测试 + 5 个真实 LLM 端到端测试,0 已知回归。

---

## 生产部署

完整 11KB 部署指南见 [docs/PRODUCTION.md](docs/PRODUCTION.md),包含:

1. 部署前清单
2. 环境变量(4 类)
3. LLM API 密钥处理
4. 存储后端选型
5. 容器化部署(Docker + K8s ingress-nginx)
6. 健康与就绪探针
7. 监控(Prometheus + OTel + 审计日志)
8. 备份与灾难恢复
9. 性能目标
10. 安全(CORS、TLS、JWT 轮换)
11. CI/CD 门禁
12. 故障响应
13. 联系人
14. 版本管理

故障响应:[docs/RUNBOOK.md](docs/RUNBOOK.md)

---

## 路线图 (v0.2.0+)

- ✅ **RS256 JWT**(多签发方 / 大规模多租户)
- ✅ **Redis 后端限流**(多副本)
- ✅ **PostgreSQL 行级安全**(Schema 层按租户隔离)
- ✅ **OpenTelemetry FastAPI 自动埋点**(按路由粒度)
- ✅ **通过 WebSocket 流式 LLM 响应**
- ✅ **GitHub App 集成**(自动 PR 审查)
- ✅ **自定义 Agent 市场**(可分享模板)

### 前瞻规划 (post-v0.3.0)

- **函数调用 / 工具调用的流式**（目前仅文本）
- **多租户 Custom Agent 市场 UI** — 用于浏览/上传自定义 Agent 的 Web 前端
- **HL7 / FHIR 适配器** — 医疗数据格式集成
- **原生 gRPC 服务器** 与 REST/WS API 并列
- **分布式任务队列** — 当前为单进程执行;添加 Celery/RQ 支持高吞吐

---

## 许可证

MIT — 见 [LICENSE](LICENSE)。

---

## 发布历史

- **v0.3.0** (2026-07-22) — 自定义 Agent 市场 + GitHub App
- **v0.2.0** (2026-07-22) — 生产强化里程碑(RS256 JWT、Redis 限流、PostgreSQL RLS、OTel FastAPI 自动埋点、WebSocket 流式 LLM)
- **v0.1.1** (2026-07-22) — Bug 修复 + 类型现代化(84 文件)
- **v0.1.0** (2026-07-09) — 首个生产级发布(22 个 PR,367 个测试通过)

完整内容见 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

## 当前状态 (v0.3.0)

- **1012** 测试通过,**5** 跳过(WebSocket TestClient 框架限制),**2** xfail
- **3** 个 known failure 在 test_*real_llm.py — 无 ANTHROPIC_API_KEY 时跳过
- 详细测试统计与历史回归趋势见 [STATUS.md](STATUS.md)
