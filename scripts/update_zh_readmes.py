# Update zh-CN and zh-TW READMEs with v0.2.0/v0.3.0 feature descriptions
import sys

LF = "\n"

V030_CN = """### v0.3.0 — 自定义 Agent 市场 + GitHub App

- **YAML 驱动的自定义智能体** — 租户通过 `examples/custom-agents/*.yaml` 定义自己的智能体，无需改代码。由 `load_from_directory()` 加载，通过 `/api/custom-agents`（list / get / run / upload / delete）对外暴露。多租户隔离；跨租户访问返回 404。
- **GitHub App Webhook 集成** — `POST /api/webhooks/github` HMAC-SHA256 签名验证，按 `X-GitHub-Delivery` 去重，在 `pull_request` opened / synchronize / reopened 时自动触发 `ReviewAgent`。可选 `GITHUB_PR_COMMENT_TOKEN` 将审查结果回贴为 PR 评论。"""

V020_CN = """### v0.2.0 — 生产强化里程碑

**RS256 JWT + JWKS 端点**：`AuthService` 自动检测：`AUTH_PRIVATE_KEY` → RS256（非对称，推荐多签发方 / 多租户）；否则 HS256（兼容旧版）。公钥通过 `GET /api/auth/jwks`（RFC 7517）分发。`scripts/gen_rsa_keys.py` 生成 2048 / 3072 / 4096 位 RSA 密钥对。

**分布式滑动窗口限流**：可插拔 `RateLimiterBackend` — `InMemoryBackend`（默认，单进程）与 `RedisBackend`（多副本安全，Lua 原子操作 ZSET）。设置 `REDIS_URL` 激活；Redis 不可达时自动回退到内存模式。

**OpenTelemetry FastAPI 自动埋点**：当 `AGENT_OTEL_ENABLED=true` 时，启动时自动调用 `FastAPIInstrumentor.instrument_app(app)`，每个请求发出按路由命名的 span。

**PostgreSQL 行级安全（RLS）**：租户隔离在数据库 Schema 层强制实施。`RLS_MIGRATION_SQL`（幂等）添加 `tenant_id` 列、索引和 RLS 策略。默认 fail-closed。`set_tenant_id()` + `_conn_with_tenant()` 每次连接 checkout 时设置 GUC。跨租户管理员使用 `BYPASSRLS` 角色。

**WebSocket 流式 LLM**：`/api/ws/llm/stream?token=&prompt=&system=` 升级 WebSocket 并逐 token 发出文本增量。`LLMRouter.stream_chunks()` 支持 Anthropic 和 OpenAI 兼容提供商。15 秒心跳检测；客户端断连时自动取消。"""

V030_TW = """### v0.3.0 — 自訂 Agent 市場 + GitHub App

- **YAML 驅動的自訂智能體** — 租戶透過 `examples/custom-agents/*.yaml` 定義自己的智能體，無需改程式碼。由 `load_from_directory()` 載入，透過 `/api/custom-agents`（list / get / run / upload / delete）對外暴露。多租戶隔離；跨租戶存取返回 404。
- **GitHub App Webhook 整合** — `POST /api/webhooks/github` HMAC-SHA256 簽章驗證，按 `X-GitHub-Delivery` 去重，在 `pull_request` opened / synchronize / reopened 時自動觸發 `ReviewAgent`。可選 `GITHUB_PR_COMMENT_TOKEN` 將審查結果回貼為 PR 評論。"""

V020_TW = """### v0.2.0 — 生產強化里程碑

**RS256 JWT + JWKS 端點**：`AuthService` 自動檢測：`AUTH_PRIVATE_KEY` → RS256（非對稱，推薦多簽發方 / 多租戶）；否則 HS256（相容舊版）。公鑰透過 `GET /api/auth/jwks`（RFC 7517）分發。`scripts/gen_rsa_keys.py` 生成 2048 / 3072 / 4096 位 RSA 金鑰對。

**分散式滑動視窗限流**：可插拔 `RateLimiterBackend` — `InMemoryBackend`（預設，單行程）與 `RedisBackend`（多副本安全，Lua 原子操作 ZSET）。設定 `REDIS_URL` 啟用；Redis 不可達時自動回退到記憶體模式。

**OpenTelemetry FastAPI 自動埋點**：當 `AGENT_OTEL_ENABLED=true` 時，啟動時自動呼叫 `FastAPIInstrumentor.instrument_app(app)`，每個請求發出按路由命名的 span。

**PostgreSQL 列級安全（RLS）**：租戶隔離在資料庫 Schema 層強制實施。`RLS_MIGRATION_SQL`（冪等）加入 `tenant_id` 欄位、索引和 RLS 策略。預設 fail-closed。`set_tenant_id()` + `_conn_with_tenant()` 每次連線 checkout 時設定 GUC。跨租戶管理員使用 `BYPASSRLS` 角色。

**WebSocket 串流 LLM**：`/api/ws/llm/stream?token=&prompt=&system=` 升級 WebSocket 並逐 token 發出文字增量。`LLMRouter.stream_chunks()` 支援 Anthropic 和 OpenAI 相容提供商。15 秒心跳偵測；用戶端斷連時自動取消。"""


def update(path, flag, end, v030, v020, label):
    with open(path, "r", encoding="utf-8") as f:
        s = f.read()
    assert flag in s, f"flag missing in {label}"
    assert end in s, f"end missing in {label}"
    before = s[:s.index(flag)]
    after = s[s.index(end):]
    new = flag.split(" (v0.1.0)")[0] + "\n\n" + v030 + LF + LF + v020 + LF + LF + "### v0.1.0 — 初始发布\n\n"
    s = before + new + after
    s = s.replace("362 个单元测试 + 5 个真实 LLM 端到端测试,0 已知回归。",
                  "**1012** 测试通过,**5** 跳过,**2** xfail,**3** 个 known failure（需 API key）。")
    with open(path, "w", encoding="utf-8") as f:
        f.write(s)
    print(f"{label} OK")


update("README.zh-CN.md",
       "## 生产级特性 (v0.1.0)",
       "## API 端点",
       V030_CN, V020_CN, "zh-CN")

update("README.zh-TW.md",
       "## 生產級特性 (v0.1.0)",
       "## API 端點",
       V030_TW, V020_TW, "zh-TW")
