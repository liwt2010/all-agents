# Agent System

[![CI](https://github.com/agent-system/agent-system/actions/workflows/ci.yml/badge.svg)](https://github.com/agent-system/agent-system/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**企業級多 Agent 協作平台** — 一個由 AI 驅動的工作流系統，內建 9 個 Agent、插件化工具、多租戶隔離和生產級安全能力。

```
用戶 → CEO Agent → 產品 Agent → 技術 Agent → 測試 Agent → 部署 Agent → DevOps Agent
                 ↘ 安全 Agent    ↘ 文件 Agent   ↘ 審查 Agent
```

## 為什麼用 Agent System？

| 跟單個 AI 聊天... | 你得到一個 **AI 團隊** |
|---|---|
| 單次回答 | 多步流水線 + 同儕評審 |
| 單次上下文視窗 | 共享記憶（MultiLinkGraph） |
| 手動切換工具 | 自動化 MCP 工具發現 |
| 無審計痕跡 | 完整審計日誌 + LLM 成本追蹤 |

## 快速開始

```bash
# 安裝
pip install -e ".[api]"

# 設定 API key（Anthropic、相容 OpenAI 或本地模型）
export ANTHROPIC_API_KEY=sk-xxx

# 執行單個 Agent
python -m agent_system run "寫一個登入功能的 PRD"

# 執行完整流水線（產品 → 技術 → 測試）
python -m agent_system pipeline "開發一個待辦事項應用"

# 啟動 API 服務
uvicorn agent_system.api.server:app --port 8000
```

## Agent 清單

| Agent | 職責 | 核心能力 |
|-------|------|----------|
| **CEO** | 總調度 | 任務分配、升級處理、流水線管理 |
| **產品** | 需求 | PRD 編寫、功能拆解、驗收標準 |
| **技術** | 實現 | 程式碼生成、架構設計、程式碼審查 |
| **測試** | 品質 | 測試生成、執行、覆蓋率分析 |
| **部署** | 維運 | 預發/生產發布、遷移執行、回滾 |
| **DevOps** | 基礎設施 | CI/CD、K8s、監控、IaC 審查 |
| **安全** | 合規 | 金鑰掃描、相依 CVE、威脅建模 |
| **文件** | 文件 | API 參考、操作手冊、ADR、更新日誌 |
| **審查** | 同儕評審 | 程式碼/設計/測試計畫審查、合併審批 |

## 功能特性

- **9 個內建 Agent** 覆蓋完整產品生命週期
- **智慧升級** — SELF / PEER / HUMAN / ESCALATE（4 路決策）
- **多向連結圖** — 11 種節點類型、23 種連結類型、時間衰減經驗記憶
- **插件化工具系統** — `@register` 裝飾器、自動發現、熱載入
- **MCP 協定** — 連線任意 MCP 相容工具伺服器
- **多租戶** — 6 級空間隔離模型（私有 → 租戶公開）
- **RBAC** — 6 種角色、7 種權限、權限組覆蓋
- **9 個自動計算指標** — 從記憶圖即時輸出
- **即時進度** — WebSocket + REST 進度輪詢 + 斷點續傳
- **安全** — 輸入校驗、金鑰檢測、限流、檔案沙箱
- **分散式鎖** — Redis 後端 + 記憶體降級

## API 端點

| 端點 | 方法 | 認證 | 描述 |
|------|------|------|------|
| `/api/health` | GET | 無 | 存活檢查 |
| `/api/ready` | GET | 無 | 就緒檢查（驗證 DB、LLM） |
| `/api/auth/token` | POST | 無 | 頒發 JWT（僅開發環境） |
| `/api/agents` | GET | JWT | 列出可用 Agent |
| `/api/tasks` | POST | JWT | 提交任務 |
| `/api/tasks/{id}` | GET | JWT | 取得任務結果 |
| `/api/tasks` | GET | JWT | 任務列表（分頁、租戶隔離） |
| `/api/tasks/{id}/progress` | GET | JWT | 即時進度 |
| `/api/ws/{id}` | WS | JWT | WebSocket 狀態流 |
| `/api/graph/stats` | GET | JWT | 圖統計 |
| `/api/metrics` | GET | JWT | Prometheus 指標 |

## 環境變數

| 變數名 | 預設值 | 說明 |
|--------|--------|------|
| `ANTHROPIC_API_KEY` | — | LLM API 金鑰（生產環境必填） |
| `AUTH_SECRET` | — | JWT 簽署金鑰（至少 32 字元） |
| `ENVIRONMENT` | `development` | `production` 啟用嚴格模式 |
| `POSTGRES_URL` | — | Postgres 連線字串 |
| `REDIS_URL` | — | Redis 連線字串 |
| `RATE_LIMIT_PER_MINUTE` | 60 | 每 IP 每分鐘請求數 |
| `ALLOWED_FILE_ROOTS` | `data,tmp,.` | 逗號分隔的檔案沙箱路徑 |
| `CORS_DEV_ORIGINS` | — | 額外 CORS 來源（開發用） |

## 生產部署

```bash
# Docker
docker-compose up --build

# Helm (K8s)
helm install agent-system ./deploy/helm \
  --set env.anthropicApiKey=$ANTHROPIC_API_KEY \
  --set env.postgresUrl=$POSTGRES_URL
```

操作手冊請參閱 [docs/RUNBOOK.md](docs/RUNBOOK.md)。

## 介面預覽

```
Dashboard:  [9 個指標卡片] [Agent 列表] [最近任務]
Submit:     [文字輸入] [Agent 選擇] → [即時進度條] [結果 JSON]
Tasks:      [按狀態篩選] [分頁列表]
Graph:      [節點/連結圖表] [年齡分佈]
Metrics:    [Sparkline 圖塊] [每 3 秒自動重新整理]
```

## 專案結構

```
src/agent_system/
├── agents/       # 9 個內建 Agent
├── api/          # FastAPI 服務
├── core/         # SmartAgent、LLM 路由、認證、RBAC、事件、快取
├── memory/       # 多向連結圖、經驗回流
├── tools/        # 插件化工具系統 + MCP 用戶端
├── storage/      # Postgres + Redis 後端
├── concurrency/  # 分散式鎖
├── migration/    # 資料遷移引擎
├── observability/ # 追蹤 + Prometheus 指標
├── config/       # 設定管理器（4 層覆蓋）
├── auth/         # JWT + RBAC
└── onboarding/   # 首次使用者體驗
```

## 效能基準

| 操作 | p50 | p95 | 樣本數 |
|------|-----|-----|--------|
| 健康檢查 | 6ms | 15ms | 200 |
| 圖節點查詢 | 0.4μs | 0.5μs | 1000 |
| 限流檢查 | 1.3μs | 2.2μs | 10000 |
| 審計日誌寫入 | 4μs | 8μs | 1000 |

## 路線圖

- [ ] WebSocket 串流式 LLM 回應
- [ ] 自訂 Agent 模板市集
- [ ] AutoGen 原生同儕討論
- [ ] OpenTelemetry SDK 匯出
- [ ] Grafana 儀表板 JSON
- [ ] GitHub App 整合（自動 PR 審查）

## 授權

MIT
