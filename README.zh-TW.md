# Agent System · 多智能體協作平臺

[![CI](https://github.com/liwt2010/all-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/liwt2010/all-agents/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![v0.3.0](https://img.shields.io/badge/release-v0.3.0-blue)](https://github.com/liwt2010/all-agents/releases/tag/v0.3.0)

> **企業級多智能體編排平臺** — 生產級 AI 智能體系統,具備共享記憶、Schema 寬容、資料溯源、分散式追蹤、OpenAPI/SDK 自動生成、端對端可觀測性。

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
| 難以擴充 | 自訂 Agent 平臺 + OpenAPI/SDK 自動生成 |

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
  liwt2010/all-agents:v0.1.0
```

存取 API:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- OpenAPI JSON: http://localhost:8000/openapi.json
- 健康檢查: http://localhost:8000/api/health
- 指標: http://localhost:8000/metrics

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

## 生產級特性 (v0.1.0)

### 核心平臺
- **9 個內建智能體**(5 個生產 + 4 個專項)
- **SmartAgent.execute()** 拆分為 checkpoint / retry / failure / escalate
- **Dataview 引擎** — 在記憶圖譜上跑類 SQL 查詢
- **四路徑解析器**:SELF / PEER / HUMAN / ESCALATE
- **AgentRegistry** 動態查找智能體
- **自訂 Agent 平臺** — Pydantic v2 友善,支援熱重載

### 記憶與學習
- **MultiLinkGraph** — 11 種節點類型、23 種連結類型、時間衰減相似度
- **經驗回饋迴圈** — 失敗的任務為後續嘗試提供參考
- **`memory_enabled` 可選關閉** — 支援臨時性工作流程

### Schema 與資料完整性
- **四級 Schema 寬容**(STRICT / LENIENT / REPAIR / WARN),支援自動修復
- **資料溯源** 每次輸出: `REAL_LLM`(置信度 0.85)/ `MOCK`(0.0)/ `LLM_FAILURE`(0.0)
- **FailureNodeLogger** — 每次 LLM 失敗都成為可稽核的圖譜節點
- **`raw_output` 兜底** — 部分結果絕不靜默失敗

### 可觀測性
- **OpenTelemetry 分散式追蹤** — DISABLED / CONSOLE / OTLP_HTTP 三種模式
  - `agent.execute` span 包含狀態 + 例外
  - FastAPI 中介層自動包裝每個 HTTP 請求
- **Prometheus 指標** — 11 個指標位於 `/metrics`
- **批量稽核日誌** — 支援保留期(預設 90 天)+ HTTP 查詢介面
- **請求 ID 透傳** 透過 `X-Request-ID` 標頭

### API 與 SDK
- **OpenAPI 3.1** 規範,中繼資料豐富(3 個 server、7 個 tag、9 個 schema)
- **Python SDK** 透過 `openapi-python-client` 自動生成
- **TypeScript SDK** 透過 `openapi-typescript-codegen` 自動生成
- **`make codegen`** 一鍵重新生成

### 安全加固
- **CORS** — 環境感知,生產環境拒絕 `*`,強制 `https://`
- **TLS** — HSTS 標頭(生產環境預設開啟)、HTTPS 重導中介層、安全 cookie 檢查
- **JWT 金鑰輪換** — `AUTH_SECRETS="kid:secret,..."` 多金鑰,零停機滾動
- **滑動視窗限流** — 按使用者 + 按作用域
- **請求體大小限制**(預設 1MB)+ **請求中金鑰偵測**
- **輸入清理** — Prompt 注入偵測(TrustLevel 感知)

### 儲存與維運
- **可插拔儲存** — JSON / SQLite / PostgreSQL
- **備份子系統** — cron + SHA-256 清單 + tar.gz + DR 演練
- **分散式鎖** — Redis 後端,記憶體兜底
- **遷移 CLI** — 切換後端不丟資料
- **多租戶隔離** — 6 空間隔離模型
- **RBAC** — 6 個角色、7 個權限、權限組覆寫

### 開發者體驗
- **生產部署指南** — [docs/PRODUCTION.md](docs/PRODUCTION.md)(11KB,15 章節)
- **故障回應手冊** — [docs/RUNBOOK.md](docs/RUNBOOK.md)
- **發佈說明** — [RELEASE_NOTES.md](RELEASE_NOTES.md)
- **CI 閘門** — 生產就緒測試套件阻擋低品質 PR

---

## API 端點

| 端點 | 方法 | 鑑權 | 說明 |
|----------|--------|------|------|
| `/api/health` | GET | 否 | 存活探針 |
| `/api/ready` | GET | 否 | 就緒探針(檢查 DB、LLM) |
| `/api/auth/token` | POST | 否 | 簽發 JWT(僅開發環境 — v0.2.0 改 RS256) |
| `/api/agents` | GET | JWT | 列出可用智能體 |
| `/api/tasks` | POST | JWT | 提交任務 |
| `/api/tasks/{id}` | GET | JWT | 取得任務結果 |
| `/api/tasks` | GET | JWT | 列出任務(分頁、按租戶隔離) |
| `/api/tasks/{id}/progress` | GET | JWT | 即時進度 |
| `/api/graph/stats` | GET | JWT | 圖譜統計 |
| `/api/graph/node/{id}` | GET | JWT | 取得指定圖譜節點 |
| `/api/audit/query` | GET | JWT | 查詢稽核日誌 |
| `/api/metrics` | GET | JWT | 應用指標(JSON) |
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

**當前狀態**:362 個單元測試 + 5 個真實 LLM 端對端測試,0 已知回歸。

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

## 路線圖 (v0.2.0+)

- [ ] **RS256 JWT**(多簽發方 / 大規模多租戶)
- [ ] **Redis 後端限流**(多副本)
- [ ] **PostgreSQL 列級安全**(Schema 層按租戶隔離)
- [ ] **OpenTelemetry FastAPI 自動埋點**(按路由粒度)
- [ ] **透過 WebSocket 串流 LLM 回應**
- [ ] **GitHub App 整合**(自動 PR 審查)
- [ ] **自訂 Agent 市場**(可分享範本)

---

## 授權

MIT — 見 [LICENSE](LICENSE)。

---

## 發佈歷史

- **v0.3.0** (2026-07-22) — 自訂 Agent 市場 + GitHub App
- **v0.2.0** (2026-07-22) — 生產強化里程碑（RS256 JWT、Redis 限流、PostgreSQL 列級安全、OTel FastAPI 自動埋點、WebSocket 串流 LLM）
- **v0.1.1** (2026-07-22) — Bug 修復 + 型別現代化（84 檔案）
- **v0.1.0** (2026-07-09) — 首個生產級發佈（22 個 PR,367 個測試通過）

完整內容見 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

## 當前狀態 (v0.3.0)

- **1012** 測試通過,**5** 跳過（WebSocket TestClient 框架限制）,**2** xfail
- **3** 個 known failure 在 test_*real_llm.py — 無 ANTHROPIC_API_KEY 時跳過
- 詳細測試統計與歷史回歸趨勢見 [STATUS.md](STATUS.md)
