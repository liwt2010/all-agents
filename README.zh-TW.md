# Agent System · 多智能體協作平臺

[![CI](https://github.com/liwt2010/all-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/liwt2010/all-agents/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![v0.5.0](https://img.shields.io/badge/release-v0.5.0-blue)](https://github.com/liwt2010/all-agents/releases/tag/v0.5.0)

> **企業級多智能體編排平臺** — 生產級 AI 智能體系統,具備共享記憶、Schema 寬容、資料溯源、分散式追蹤、OpenAPI/SDK 自動生成、端對端可觀測性、原生 gRPC 傳輸、流式工具呼叫事件。

```
User → CEO Agent → Product Agent → Tech Agent → Test Agent → Deploy Agent
                    ↘ Security    ↘ Docs      ↘ Review       ↘ DevOps
```

📖 **其他語言**: [English](README.md) · [简体中文](README.zh-CN.md)

---

## 為什麼選擇 Agent System?

| 單一 AI | Agent System |
|---|---|
| 一次性回答 | 多步驟流水線 + 同儕審查 |
| 單一上下文視窗 | 共享記憶圖譜(11 種節點類型、23 種連結類型) |
| 手動切換工具 | 自動探索 MCP 工具註冊表 |
| 沒有稽核追蹤 | 完整稽核日誌 + LLM 成本追蹤 |
| 靜默失敗 | 資料溯源標籤(REAL_LLM / MOCK / LLM_FAILURE) |
| 維運不透明 | Prometheus + OpenTelemetry 開箱即用 |
| 難以擴充 | 自訂 Agent 平臺 + OpenAPI/SDK 自動生成 + 原生 gRPC 傳輸 |

---

## 快速開始

```bash
# 1. 安裝
pip install -e ".[api,storage]"

# 2. 設定
export ANTHROPIC_API_KEY=sk-xxx           # 或 OPENAI_API_KEY
export AUTH_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")

# 3. 執行單一 Agent
python -m agent_system run "寫一個登入功能的 PRD"

# 4. 執行完整流水線
python -m agent_system pipeline "構建一個 todo 應用"

# 5. 啟動 API 服務
uvicorn agent_system.api.server:app --host 0.0.0.0 --port 8000
```

或使用 Docker:

```bash
docker run -d --name agent-system \
  -e AUTH_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))") \
  -e ANTHROPIC_API_KEY=sk-xxx \
  -p 8000:8000 \
  liwt2010/all-agents:v0.5.0
```

存取 API:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- OpenAPI JSON: http://localhost:8000/openapi.json
- 健康檢查: http://localhost:8000/api/health
- 指標: http://localhost:8000/metrics
- gRPC: `localhost:50051`(執行 `python -m agent_system.grpc.server`;詳見 [docs/GRPC.md](docs/GRPC.md))

---

## 智能體清單

| Agent | 角色 | 核心能力 |
|-------|------|----------|
| **CEO** | 編排者 | 任務分派、四路徑升級、流水線管理 |
| **Product** | 需求 | PRD 編寫、功能拆解、驗收標準 |
| **Tech** | 實作 | 程式碼生成、架構設計、程式碼審查 |
| **Test** | 品質 | 測試生成、執行、覆蓋率分析 |
| **Deploy** | 維運 | 預發/生產發佈、遷移執行、回滾 |
| **Security** | 合規 | 金鑰掃描、CVE、威脅建模 |
| **Docs** | 文件 | API 參考、維運手冊、ADR、變更日誌 |
| **Review** | 同儕審查 | 程式碼/設計/測試方案審查、合併核准 |
| **DevOps** | 基礎設施 | CI/CD、K8s、監控、IaC 審查 |

---

## 生產級特性

### v0.5.0 — 原生 gRPC 傳輸

新的 gRPC 傳輸層與現有 REST + WebSocket API 並行執行,共用同一行程中的 `TaskStore` 和 `LLMRouter`。透過 HTTP 提交的任務立刻可經 gRPC 看到(反之亦然)。

- **4 個 RPC** 定義於 [`src/agent_system/grpc/proto/agent_system.proto`](src/agent_system/grpc/proto/agent_system.proto):
  - `SubmitTask(SubmitTaskRequest) returns (Task)`
  - `GetTask(GetTaskRequest) returns (Task)` — 缺失或租戶不符時回傳 `NOT_FOUND`
  - `ListTasks(ListTasksRequest) returns (stream ListTasksResponse)` — server-streaming
  - `StreamLLM(StreamLLMRequest) returns (stream LLMEvent)` — server-streaming,文字 **加** 工具呼叫事件(與 WebSocket 線格式一致)
- **傳輸無關的 handler** — `GrpcServiceHandler` 接收 dict、產出 dict。生成的 servicer 只是一層把 protobuf 與 dict 互譯的薄殼,未來傳輸(JSON-RPC、gRPC-Web、行程內匯流排)可直接複用。
- **產生的 `_pb2` 模組已加入 .gitignore** — 200+ KB 自動生成程式碼會膨脹倉庫並在每次 proto 變更時帶來合併雜訊。首次 checkout 跑一次 `python -m agent_system.grpc.codegen` 即可,冪等。
- **25 個新測試** 在 `tests/test_grpc_handlers.py` 直接驅動 handler 類別,不需要 grpcio;契約是 dict 形態,由產生的 servicer 負責翻譯。已透過真實 gRPC channel 端到端驗證(SubmitTask / GetTask / ListTasks / NOT_FOUND 狀態)。

執行:

```bash
pip install grpcio grpcio-tools
python -m agent_system.grpc.codegen          # 一次性
python -m agent_system.grpc.server            # 預設 :50051
AGENT_GRPC_PORT=50052 python -m agent_system.grpc.server
```

完整線格式、用戶端範例與架構說明見 [docs/GRPC.md](docs/GRPC.md)。

### v0.4.0 — 流式工具呼叫事件

生產級智能體不只會輸出文字——還會呼叫工具(搜尋、檢索、執行程式碼……)。v0.2.0 的串流端點只暴露文字增量,所以聊天 UI 在 LLM 工作時只能顯示 "...",執行器在串流中也看不到工具呼叫。v0.4.0 將工具呼叫提升為一級串流事件。

- **`LLMRouter.stream_events()` async 產生器** — 單通道吐出 `StreamEvent` dataclass,kind 含:
  - `text` — 文字增量(等同於舊 `chunk` 事件)
  - `tool_start` — Provider 開啟了一次工具呼叫(`tool`、`id` 已設)
  - `tool_input` — 呼叫參數的 JSON 片段
  - `tool_end` — 工具參數已收齊
  - `tool_result` — Agent 執行器對該工具的回傳
  - `done` — 結束,帶聚合後的 `LLMUsage`
  - `error` — Provider 或傳輸錯誤
- **兩種 Provider 均支援**:
  - **Anthropic** — `content_block_start`(tool_use) → `tool_start`;`input_json_delta` → `tool_input`;`content_block_stop` → `tool_end`
  - **OpenAI** — `delta.tool_calls[].id` 出現 → `tool_start`;`.function.arguments` 增量 → `tool_input`;同一 index 不再有增量 → `tool_end`
- **WS 端點橋接為 JSON 框** — `/api/ws/llm/stream` 現在會送出 `{"type":"tool_start","data":{"tool":"search","id":"..."}}`、`{"type":"tool_input",...}` 等;舊 `chunk` 事件保留以相容。
- **9 個新測試** 在 `tests/test_llm_stream_events.py` 涵蓋事件語意、mock 模式串流、Anthropic / OpenAI 工具呼叫序列,以及 `stream_chunks()` 相容墊片;同時修正一個潛在的 `estimate_cost` 參數順序 bug。

### v0.3.0 — 自訂 Agent 市場 + GitHub App

- **YAML 驅動的自訂智能體** — 租戶透過 `examples/custom-agents/*.yaml` 定義自己的智能體，無需改程式碼。由 `load_from_directory()` 載入，透過 `/api/custom-agents`（list / get / run / upload / delete）對外暴露。多租戶隔離；跨租戶存取返回 404。
- **GitHub App Webhook 整合** — `POST /api/webhooks/github` HMAC-SHA256 簽章驗證，按 `X-GitHub-Delivery` 去重，在 `pull_request` opened / synchronize / reopened 時自動觸發 `ReviewAgent`。可選 `GITHUB_PR_COMMENT_TOKEN` 將審查結果回貼為 PR 評論。

### v0.2.0 — 生產強化里程碑

**RS256 JWT + JWKS 端點**：`AuthService` 自動檢測：`AUTH_PRIVATE_KEY` → RS256（非對稱，推薦多簽發方 / 多租戶）；否則 HS256（相容舊版）。公鑰透過 `GET /api/auth/jwks`（RFC 7517）分發。`scripts/gen_rsa_keys.py` 生成 2048 / 3072 / 4096 位 RSA 金鑰對。

**分散式滑動視窗限流**：可插拔 `RateLimiterBackend` — `InMemoryBackend`（預設，單行程）與 `RedisBackend`（多副本安全，Lua 原子操作 ZSET）。設定 `REDIS_URL` 啟用；Redis 不可達時自動回退到記憶體模式。

**OpenTelemetry FastAPI 自動埋點**：當 `AGENT_OTEL_ENABLED=true` 時，啟動時自動呼叫 `FastAPIInstrumentor.instrument_app(app)`，每個請求發出按路由命名的 span。

**PostgreSQL 列級安全（RLS）**：租戶隔離在資料庫 Schema 層強制實施。`RLS_MIGRATION_SQL`（冪等）加入 `tenant_id` 欄位、索引和 RLS 策略。預設 fail-closed。`set_tenant_id()` + `_conn_with_tenant()` 每次連線 checkout 時設定 GUC。跨租戶管理員使用 `BYPASSRLS` 角色。

**WebSocket 串流 LLM**：`/api/ws/llm/stream?token=&prompt=&system=` 升級 WebSocket 並逐 token 發出文字增量。`LLMRouter.stream_chunks()` 支援 Anthropic 和 OpenAI 相容提供商。15 秒心跳偵測；用戶端斷連時自動取消。

### v0.1.0 — 初始发布

## API 端點

| 端點 | 方法 | 鑑權 | 說明 |
|----------|--------|------|------|
| `/api/health` | GET | 否 | 存活探針 |
| `/api/ready` | GET | 否 | 就緒探針(檢查 DB、LLM) |
| `/api/auth/token` | POST | 否 | 簽發 JWT(僅開發環境 — v0.2.0 改 RS256) |
| `/api/auth/jwks` | GET | 否 | RS256 公鑰(RFC 7517 JWKS) |
| `/api/agents` | GET | JWT | 列出可用智能體 |
| `/api/tasks` | POST | JWT | 提交任務 |
| `/api/tasks/{id}` | GET | JWT | 取得任務結果 |
| `/api/tasks` | GET | JWT | 列出任務(分頁、按租戶隔離) |
| `/api/tasks/{id}/progress` | GET | JWT | 即時進度 |
| `/api/graph/stats` | GET | JWT | 圖譜統計 |
| `/api/graph/node/{id}` | GET | JWT | 取得指定圖譜節點 |
| `/api/audit/query` | GET | JWT | 查詢稽核日誌 |
| `/api/metrics` | GET | JWT | 應用指標(JSON) |
| `/api/custom-agents` | GET | JWT | 列出自訂智能體(按租戶) |
| `/api/custom-agents/{id}` | GET | JWT | 自訂智能體詳情 |
| `/api/custom-agents/{id}/run` | POST | JWT | 呼叫自訂智能體 |
| `/api/custom-agents:upload` | POST | JWT(admin) | 註冊 YAML 智能體 |
| `/api/custom-agents/{id}` | DELETE | JWT(admin) | 刪除自訂智能體 |
| `/api/ws/llm/stream` | WS | JWT(query) | 流式 LLM token + 工具呼叫事件 |
| `/api/webhooks/github` | POST | HMAC | GitHub App webhook 接收器 |
| **gRPC `:50051`** | — | (見 GRPC.md) | SubmitTask / GetTask / ListTasks / StreamLLM |
| `/metrics` | GET | 否 | Prometheus 抓取端點 |

完整 OpenAPI 規範: [/openapi.json](http://localhost:8000/openapi.json)

---

## 設定

| 環境變數 | 必填 | 預設值 | 說明 |
|---------|----------|---------|------|
| `ANTHROPIC_API_KEY` | 是* | — | LLM API 金鑰(Anthropic / OpenAI 相容) |
| `AUTH_SECRET` | 是 | — | JWT 簽章金鑰(32+ 字元)— 或使用 `AUTH_SECRETS` 進行輪換 |
| `ENVIRONMENT` | 否 | `development` | 設為 `production` 啟用嚴格模式 |
| `LLM_PROVIDER` | 否 | `anthropic` | `anthropic` / `openai` / `mock` |
| `LLM_MODEL` | 否 | (provider 預設) | 模型名稱(如 `claude-3-5-sonnet`) |
| `CORS_ALLOWED_ORIGINS` | 生產必填 | localhost:5173(開發) | 逗號分隔的 https:// 來源 |
| `AGENT_OTEL_ENABLED` | 否 | `false` | 啟用 OpenTelemetry 追蹤 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 啟用 OTEL 時 | `http://localhost:4318` | OTLP/HTTP collector 位址 |
| `AGENT_GRPC_PORT` | 否 | `50051` | gRPC 伺服器綁定埠(可選) |
| `STORAGE_BACKEND` | 否 | `json` | `json` / `sqlite` / `postgres` |
| `POSTGRES_URL` | postgres 時 | — | PostgreSQL 連線字串 |
| `REDIS_URL` | 否 | — | Redis 位址(用於分散式鎖) |
| `RATE_LIMIT_PER_MINUTE` | 否 | 120 | 每使用者每分鐘請求數(預設作用域) |
| `AGENT_RATE_LIMIT_ENABLED` | 否 | `true` | 開關滑動視窗限流 |
| `AGENT_AUDIT_RETENTION_DAYS` | 否 | 90 | 稽核日誌保留期 |
| `AGENT_BACKUP_CRON` | 否 | `0 2 * * *` | 備份排程(cron 格式) |
| `TLS_REDIRECT_ENABLED` | 否 | `false` | 啟用 HTTP→HTTPS 301 |
| `TLS_HSTS_ENABLED` | 否 | `true`(生產) | 新增 HSTS 標頭 |
| `MAX_REQUEST_BYTES` | 否 | `1048576` | 請求體上限(1MB) |
| `ALLOWED_FILE_ROOTS` | 否 | `data,tmp` | 檔案沙箱根目錄 |

*開發環境使用 `mock` provider 時無需金鑰。

完整註解清單見 [.env.example](.env.example)。

---

## 專案結構

```
src/agent_system/
├── agents/          # 9 個內建智能體(Product、Tech、Test、Deploy、CEO、Security、Docs、Review、DevOps)
├── api/             # FastAPI 服務(OpenAPI、中介層、WebSocket)
├── grpc/                # 原生 gRPC 傳輸(v0.5.0)— codegen、handlers、server
│   ├── proto/agent_system.proto   # 單一真相源
│   ├── codegen.py                 # `python -m agent_system.grpc.codegen`
│   ├── handlers.py                # 傳輸無關的 GrpcServiceHandler
│   └── server.py                  # `python -m agent_system.grpc.server`
├── auth/            # JWT + RBAC + 多租戶上下文(從 core/auth/ 重新匯出)
├── codegen/         # OpenAPI 規範匯出 + Python/TypeScript SDK 生成器(PR-15)
├── concurrency/     # 分散式鎖(Redis + 記憶體兜底)
├── config/          # ConfigManager(4 層設定覆寫)
├── core/            # SmartAgent、LLM 路由、安全中介層、稽核
│   ├── auth/        # JWT、RBAC、TenantContext
│   ├── observability/  # DataProvenance、tracing
│   ├── rate_limit/  # 滑動視窗限流器 + 註冊表(PR-12)
│   ├── backup/      # 清單 + 排程器 + 還原 + 保留(PR-13)
│   ├── security/    # CORS + TLS + 金鑰輪換(PR-16)
│   └── ...
├── memory/          # MultiLinkGraph、經驗回饋迴圈、embeddings
├── observability/   # Prometheus 指標、OTel 匯出器 + 中介層(PR-14)
├── storage/         # JSON / SQLite / PostgreSQL 後端 + 遷移 CLI
├── tools/           # 外掛工具系統 + MCP 用戶端
├── migration/       # 資料遷移引擎
└── onboarding/      # 首次使用者體驗
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
make codegen      # OpenAPI 規範 + Python SDK
make codegen-ts   # 加上 TypeScript SDK(需要 Node.js)
```

---

## 測試

```bash
# 單元測試(始終執行,無需 LLM)
pytest tests/ -q --ignore=tests/test_*real_llm.py

# 真實 LLM 端對端測試(需要 API 金鑰)
ANTHROPIC_API_KEY=sk-xxx pytest tests/test_*real_llm.py -v

# 生產就緒閘門(CI 始終執行)
pytest tests/test_production_readiness.py -v
```

**當前狀態**:**1048** 測試通過,**5** 跳過,**2** xfail,**3** 個 known failure(需 API key);含 **25** 個新 gRPC handler 測試,0 已知回歸。

---

## 生產部署

完整 11KB 部署指南見 [docs/PRODUCTION.md](docs/PRODUCTION.md),包含:

1. 部署前清單
2. 環境變數(4 類)
3. LLM API 金鑰處理
4. 儲存後端選型
5. 容器化部署(Docker + K8s ingress-nginx)
6. 健康與就緒探針
7. 監控(Prometheus + OTel + 稽核日誌)
8. 備份與災難還原
9. 效能目標
10. 安全(CORS、TLS、JWT 輪換)
11. CI/CD 閘門
12. 故障回應
13. 聯絡人
14. 版本管理

故障回應:[docs/RUNBOOK.md](docs/RUNBOOK.md)

---

## 路線圖

### v0.3.0 — 已交付 ✅

- ✅ **自訂 Agent 市場** — YAML 驅動的租戶級智能體，HTTP API 支援 list / get / run / upload / delete
- ✅ **GitHub App 整合** — Webhook 接收器 + 自動 PR 審查 dispatch via ReviewAgent

### v0.2.0 — 已交付 ✅

- ✅ **RS256 JWT**(多簽發方 / 大規模多租戶)
- ✅ **Redis 後端限流**(多副本)
- ✅ **PostgreSQL 列級安全**(Schema 層按租戶隔離)
- ✅ **OpenTelemetry FastAPI 自動埋點**(按路由粒度)
- ✅ **透過 WebSocket 串流 LLM 回應**
- ✅ **GitHub App 整合**(自動 PR 審查)
- ✅ **自訂 Agent 市場**(可分享範本)

### 前瞻規劃 (post-v0.3.0)

- **函式呼叫 / 工具呼叫的串流**（目前僅文字）
- **多租戶 Custom Agent 市場 UI** — 用於瀏覽/上傳自訂 Agent 的 Web 前端
- **HL7 / FHIR 配接器** — 醫療資料格式整合
- **原生 gRPC 伺服器** 與 REST/WS API 並列
- **分散式任務佇列** — 目前為單行程執行;加入 Celery/RQ 支援高吞吐

---

## 授權

MIT — 見 [LICENSE](LICENSE)。

---

## 發佈歷史

- **v0.5.0** (2026-07-24) — 原生 gRPC 傳輸 — 4 個 RPC(`SubmitTask` / `GetTask` / `ListTasks` / `StreamLLM`),ListTasks 與 StreamLLM 為 server-streaming;`.proto` 為單一真相源;含 25 個新測試與真實 channel 端到端驗證
- **v0.4.0** (2026-07-22) — 流式工具呼叫事件 — `LLMRouter.stream_events()` 將 `tool_start` / `tool_input` / `tool_end` / `tool_result` 提升為一級事件(Anthropic + OpenAI);WS 端點橋接為 JSON 框,舊 `chunk` 事件保留;含 9 個新測試並修復 `estimate_cost` 參數順序 bug
- **v0.3.0** (2026-07-22) — 自訂 Agent 市場 + GitHub App
- **v0.2.0** (2026-07-22) — 生產強化里程碑（RS256 JWT、Redis 限流、PostgreSQL 列級安全、OTel FastAPI 自動埋點、WebSocket 串流 LLM）
- **v0.1.1** (2026-07-22) — Bug 修復 + 型別現代化（84 檔案）
- **v0.1.0** (2026-07-09) — 首個生產級發佈（22 個 PR,367 個測試通過）

完整內容見 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

## 當前狀態 (v0.5.0)

- **1048** 測試通過,**5** 跳過(WebSocket TestClient 框架限制),**2** xfail
- **25** 個新 gRPC handler 測試
- **3** 個 known failure 在 test_*real_llm.py — 無 ANTHROPIC_API_KEY 時跳過
- 詳細測試統計與歷史回歸趨勢見 [STATUS.md](STATUS.md)
