# Agent System · 多智能体协作平台

[![CI](https://github.com/liwt2010/all-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/liwt2010/all-agents/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![v0.6.0](https://img.shields.io/badge/release-v0.6.0-blue)](https://github.com/liwt2010/all-agents/releases/tag/v0.6.0)

> **企业级多智能体编排平台** — 生产级 AI 智能体系统,具备共享记忆、Schema 宽容、数据溯源、分布式追踪、OpenAPI/SDK 自动生成、端到端可观测性、原生 gRPC 传输、流式工具调用事件。

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
| 难以扩展 | 自定义 Agent 平台 + OpenAPI/SDK 自动生成 + 原生 gRPC 传输 |

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
  liwt2010/all-agents:v0.6.0
```

访问 API:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- OpenAPI JSON: http://localhost:8000/openapi.json
- 健康检查: http://localhost:8000/api/health
- 指标: http://localhost:8000/metrics
- gRPC: `localhost:50051`(运行 `python -m agent_system.grpc.server`;详见 [docs/GRPC.md](docs/GRPC.md))

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

## 生产级特性

### v0.6.0 — Task 协作原语

多用户 / 多 Agent 部署之前无法安全地共享任务 —— 两个客户端可能并发编辑同一行,没有 claim / hand-off,`ARCHITECTURE.md` §11 中的 6-space 可见性模型只在文档中。v0.6.0 把 §5 / §11 / §14 的协作设计落地为代码。

- **`TaskRecord` 增加 4 个字段**:
  - `owner_id` —— 任务创建者,**不可变**(白名单在 `TaskStore.update_fields` 中强制)。
  - `assignee_id` —— 当前负责人,通过 `claim` / `handoff` 流转。
  - `version` —— CAS 计数器,每次 `update_fields` 自增。
  - `visibility` —— `private` / `perm_group` / `group` / `project` / `external` / `tenant_public` 之一(默认 `private`,最保守)。
- **`TaskStore.update_fields(id, expected_version, **fields)`** —— compare-and-swap 更新。版本不匹配时抛 `VersionConflict`(携带当前记录)。Postgres 用 `UPDATE ... WHERE id=:id AND version=:ev RETURNING`;InMemory 在 dict 层做 CAS。
- **3 个新 REST 端点**:
  - `POST /api/tasks/{id}/claim` —— 设置 `assignee_id = me`。PRIVATE:只有 owner。其它可见性:任何有读权限的用户。
  - `POST /api/tasks/{id}/handoff` —— 修改 `assignee_id`。Body: `{to_user_id, expected_version?, reason?}`。Owner / 当前 assignee / platform_admin 可用。
  - `GET /api/tasks/{id}/events` —— Task 范围的审计时间线(`task.claimed`、`task.handoff`、`task.completed`、……)。
- **`AccessControl` 已接入 task 路由** —— `_to_user_ctx`、`_record_to_resource`、`_ensure_can_read`。`list_tasks` 在内存中过滤(SQL 下推留 TODO)。
- **`AuditLogEntry.task_id` 字段** —— 快速 task-scoped 查询;`/api/audit/query?task_id=` 工作;老条目(`resource_type="task"` + `resource_id`)仍能匹配。
- **3 个入口的归属**:
  - **gRPC `SubmitTask`** —— 从 `context.invocation_metadata()` 读 `x-user-id` / `x-tenant_id`(`.proto` 不变,wire 兼容)。缺省为 `"system"`。
  - **GitHub webhook** —— `TaskContext.metadata.owner_id` = `GITHUB_BOT_USER_ID` 环境变量(默认 `github-bot`);`visibility="project"`;`project_ids=["pr:{repo}"]`。
  - **Custom Agent `/run`** —— `owner_id` = JWT user;每次调用写审计日志。

### v0.5.0 — 原生 gRPC 传输

新的 gRPC 传输层与现有 REST + WebSocket API 并行运行,共享同一进程内的 `TaskStore` 和 `LLMRouter`。通过 HTTP 提交的任务立刻可通过 gRPC 看见(反之亦然)。

- **4 个 RPC** 定义于 [`src/agent_system/grpc/proto/agent_system.proto`](src/agent_system/grpc/proto/agent_system.proto):
  - `SubmitTask(SubmitTaskRequest) returns (Task)`
  - `GetTask(GetTaskRequest) returns (Task)` — 缺失或租户不匹配时返回 `NOT_FOUND`
  - `ListTasks(ListTasksRequest) returns (stream ListTasksResponse)` — 服务端流式
  - `StreamLLM(StreamLLMRequest) returns (stream LLMEvent)` — 服务端流式,文本 **加** 工具调用事件(与 WebSocket 线协议一致)
- **传输无关的 handler** — `GrpcServiceHandler` 接收 dict、产出 dict。生成的 servicer 仅是把 protobuf 与 dict 互译的薄壳,未来传输(JSON-RPC、gRPC-Web、进程内总线)可直接复用。
- **生成的 `_pb2` 模块已 gitignore** — 200+ KB 自动生成代码会膨胀仓库并在每次 proto 改动时制造合并噪音。首次 checkout 时运行一次 `python -m agent_system.grpc.codegen` 即可,幂等。
- **25 个新测试** 在 `tests/test_grpc_handlers.py` 直接驱动 handler 类,无需 grpcio;契约是 dict 形态,由生成的 servicer 完成翻译。已经过真实 gRPC channel 端到端验证(SubmitTask / GetTask / ListTasks / NOT_FOUND 状态)。

运行:

```bash
pip install grpcio grpcio-tools
python -m agent_system.grpc.codegen          # 一次性
python -m agent_system.grpc.server            # 默认 :50051
AGENT_GRPC_PORT=50052 python -m agent_system.grpc.server
```

完整线协议、客户端示例与架构说明见 [docs/GRPC.md](docs/GRPC.md)。

### v0.4.0 — 流式工具调用事件

生产级智能体不只是输出文本——还会调用工具(搜索、检索、代码执行……)。v0.2.0 流式端点仅暴露文本增量,所以聊天 UI 在 LLM 工作时只能显示 "...",执行器在流中也看不到工具调用。v0.4.0 将工具调用提升为一等流事件。

- **`LLMRouter.stream_events()` async 生成器** — 单通道产出 `StreamEvent` dataclass,kind 包含:
  - `text` — 文本增量(等同于旧 `chunk` 事件)
  - `tool_start` — Provider 打开了一次工具调用(`tool`、`id` 已设)
  - `tool_input` — 调用参数的 JSON 片段
  - `tool_end` — 工具参数已收齐
  - `tool_result` — Agent 执行器对该工具的返回
  - `done` — 终结,携带聚合后的 `LLMUsage`
  - `error` — Provider 或传输错误
- **两种 Provider 均支持**:
  - **Anthropic** — `content_block_start`(tool_use) → `tool_start`;`input_json_delta` → `tool_input`;`content_block_stop` → `tool_end`
  - **OpenAI** — `delta.tool_calls[].id` 出现 → `tool_start`;`.function.arguments` 增量 → `tool_input`;同一 index 不再有增量 → `tool_end`
- **WS 端点桥接为 JSON 帧** — `/api/ws/llm/stream` 现在发出 `{"type":"tool_start","data":{"tool":"search","id":"..."}}`、`{"type":"tool_input",...}` 等;旧 `chunk` 事件仍保留以兼容。
- **9 个新测试** 在 `tests/test_llm_stream_events.py` 覆盖事件形态语义、mock 模式流式、Anthropic / OpenAI 工具调用序列,以及 `stream_chunks()` 兼容垫片;同时修复了一个潜伏的 `estimate_cost` 参数顺序 bug。

### v0.3.0 — 自定义 Agent 市场 + GitHub App

- **YAML 驱动的自定义智能体** — 租户通过 `examples/custom-agents/*.yaml` 定义自己的智能体，无需改代码。由 `load_from_directory()` 加载，通过 `/api/custom-agents`（list / get / run / upload / delete）对外暴露。多租户隔离；跨租户访问返回 404。
- **GitHub App Webhook 集成** — `POST /api/webhooks/github` HMAC-SHA256 签名验证，按 `X-GitHub-Delivery` 去重，在 `pull_request` opened / synchronize / reopened 时自动触发 `ReviewAgent`。可选 `GITHUB_PR_COMMENT_TOKEN` 将审查结果回贴为 PR 评论。

### v0.2.0 — 生产强化里程碑

**RS256 JWT + JWKS 端点**：`AuthService` 自动检测：`AUTH_PRIVATE_KEY` → RS256（非对称，推荐多签发方 / 多租户）；否则 HS256（兼容旧版）。公钥通过 `GET /api/auth/jwks`（RFC 7517）分发。`scripts/gen_rsa_keys.py` 生成 2048 / 3072 / 4096 位 RSA 密钥对。

**分布式滑动窗口限流**：可插拔 `RateLimiterBackend` — `InMemoryBackend`（默认，单进程）与 `RedisBackend`（多副本安全，Lua 原子操作 ZSET）。设置 `REDIS_URL` 激活；Redis 不可达时自动回退到内存模式。

**OpenTelemetry FastAPI 自动埋点**：当 `AGENT_OTEL_ENABLED=true` 时，启动时自动调用 `FastAPIInstrumentor.instrument_app(app)`，每个请求发出按路由命名的 span。

**PostgreSQL 行级安全（RLS）**：租户隔离在数据库 Schema 层强制实施。`RLS_MIGRATION_SQL`（幂等）添加 `tenant_id` 列、索引和 RLS 策略。默认 fail-closed。`set_tenant_id()` + `_conn_with_tenant()` 每次连接 checkout 时设置 GUC。跨租户管理员使用 `BYPASSRLS` 角色。

**WebSocket 流式 LLM**：`/api/ws/llm/stream?token=&prompt=&system=` 升级 WebSocket 并逐 token 发出文本增量。`LLMRouter.stream_chunks()` 支持 Anthropic 和 OpenAI 兼容提供商。15 秒心跳检测；客户端断连时自动取消。

### v0.1.0 — 初始发布

## API 端点

| 端点 | 方法 | 鉴权 | 说明 |
|----------|--------|------|------|
| `/api/health` | GET | 否 | 存活探针 |
| `/api/ready` | GET | 否 | 就绪探针(检查 DB、LLM) |
| `/api/auth/token` | POST | 否 | 签发 JWT(仅开发环境 — v0.2.0 改 RS256) |
| `/api/auth/jwks` | GET | 否 | RS256 公钥(RFC 7517 JWKS) |
| `/api/agents` | GET | JWT | 列出可用智能体 |
| `/api/tasks` | POST | JWT | 提交任务 |
| `/api/tasks/{id}` | GET | JWT | 获取任务结果 |
| `/api/tasks` | GET | JWT | 列出任务(分页、按租户隔离) |
| `/api/tasks/{id}/progress` | GET | JWT | 实时进度 |
| `/api/graph/stats` | GET | JWT | 图谱统计 |
| `/api/graph/node/{id}` | GET | JWT | 获取指定图谱节点 |
| `/api/audit/query` | GET | JWT | 查询审计日志 |
| `/api/metrics` | GET | JWT | 应用指标(JSON) |
| `/api/custom-agents` | GET | JWT | 列出自定义智能体(按租户) |
| `/api/custom-agents/{id}` | GET | JWT | 自定义智能体详情 |
| `/api/custom-agents/{id}/run` | POST | JWT | 调用自定义智能体 |
| `/api/custom-agents:upload` | POST | JWT(admin) | 注册 YAML 智能体 |
| `/api/custom-agents/{id}` | DELETE | JWT(admin) | 删除自定义智能体 |
| `/api/ws/llm/stream` | WS | JWT(query) | 流式 LLM token + 工具调用事件 |
| `/api/webhooks/github` | POST | HMAC | GitHub App webhook 接收器 |
| **gRPC `:50051`** | — | (见 GRPC.md) | SubmitTask / GetTask / ListTasks / StreamLLM |
| `/api/tasks/{id}/claim` | POST | JWT | 认领任务(assignee_id = 我) |
| `/api/tasks/{id}/handoff` | POST | JWT | 把任务转交给其他用户 |
| `/api/tasks/{id}/events` | GET | JWT | 任务协作时间线(审计) |
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
| `AGENT_GRPC_PORT` | 否 | `50051` | gRPC 服务端绑定端口(可选) |
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
├── grpc/                # 原生 gRPC 传输(v0.5.0)— codegen、handlers、server
│   ├── proto/agent_system.proto   # 真相之源
│   ├── codegen.py                 # `python -m agent_system.grpc.codegen`
│   ├── handlers.py                # 传输无关的 GrpcServiceHandler
│   └── server.py                  # `python -m agent_system.grpc.server`
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

**当前状态**:**1105** 测试通过,**5** 跳过,**2** xfail,**3** 个 known failure(需 API key);v0.6.0 新增 50+ 测试,0 已知回归。

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
- ✅ **流式工具调用事件** — `LLMRouter.stream_events()` 把工具调用作为一等事件暴露(Anthropic + OpenAI)
- ✅ **原生 gRPC 传输** — 4 个 RPC(`SubmitTask` / `GetTask` / `ListTasks` / `StreamLLM`),ListTasks 与 StreamLLM 服务端流式
- ✅ **Task 协作原语** — `owner_id` / `assignee_id` / `version` / `visibility` 字段;CAS via `TaskStore.update_fields`;`claim` / `handoff` / `events` 端点;AccessControl 接入 task 路由;`AuditLogEntry.task_id`;gRPC / webhook / custom-agent 归属

### 前瞻规划 (post-v0.6.0)

- **多租户 Custom Agent 市场 UI** — 用于浏览/上传自定义 Agent 的 Web 前端
- **HL7 / FHIR 适配器** — 医疗数据格式集成
- **gRPC 拦截器** — auth + rate-limit 中间件与 HTTP 层对等(目前 x-user-id metadata 是唯一契约)
- **分布式任务队列** — 当前为单进程执行;加入 Celery/RQ 支持高吞吐

---

## 许可证

MIT — 见 [LICENSE](LICENSE)。

---

## 发布历史

- **v0.6.0** (2026-07-24) — Task 协作原语
  - `TaskRecord` 增加 `owner_id` / `assignee_id` / `version` / `visibility`
  - `TaskStore.update_fields` 带 CAS(抛 `VersionConflict` 含当前记录)
  - 3 个新端点:`POST /api/tasks/{id}/claim`、`.../handoff`、`GET .../events`
  - AccessControl 接入 task 路由(private / shared_with / admin)
  - `AuditLogEntry.task_id` + `?task_id=` 查询过滤
  - gRPC / webhook / custom-agent 归属
- **v0.5.0** (2026-07-24) — 原生 gRPC 传输 — 4 个 RPC(`SubmitTask` / `GetTask` / `ListTasks` / `StreamLLM`),ListTasks 与 StreamLLM 为服务端流式;`.proto` 为单一真相源;含 25 个新测试与真实 channel 端到端验证
- **v0.4.0** (2026-07-22) — 流式工具调用事件 — `LLMRouter.stream_events()` 将 `tool_start` / `tool_input` / `tool_end` / `tool_result` 提升为一等事件(Anthropic + OpenAI);WS 端点桥接为 JSON 帧,旧 `chunk` 事件保留;含 9 个新测试并修复 `estimate_cost` 参数顺序 bug
- **v0.3.0** (2026-07-22) — 自定义 Agent 市场 + GitHub App
- **v0.2.0** (2026-07-22) — 生产强化里程碑(RS256 JWT、Redis 限流、PostgreSQL RLS、OTel FastAPI 自动埋点、WebSocket 流式 LLM)
- **v0.1.1** (2026-07-22) — Bug 修复 + 类型现代化(84 文件)
- **v0.1.0** (2026-07-09) — 首个生产级发布(22 个 PR,367 个测试通过)

完整内容见 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

## 当前状态 (v0.6.0)

- **1105** 测试通过,**5** 跳过(WebSocket TestClient 框架限制),**2** xfail
- v0.6.0 新增 50+ 测试(CAS、可见性、claim、handoff、events、audit task_id、gRPC metadata owner、webhook + custom-agent 归属)
- **3** 个 known failure 在 test_*real_llm.py — 无 ANTHROPIC_API_KEY 时跳过
- 详细测试统计与历史回归趋势见 [STATUS.md](STATUS.md)
