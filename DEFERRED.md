# Deferred Tasks (历史快照)

> 本文件记录被用户搁置或被新实现取代的任务。保留下来是因为：
> 1. 它们代表了某个特定时间点的决策；
> 2. 它们对 git archaeology 有价值（git log 不一定能立刻还原上下文）。
>
> 活跃的路线图见 [docs/TODO.md](TODO.md)；最新进展见 [CHANGELOG.md](../CHANGELOG.md)。

---

## DEFERRED-001: GitHub Actions CI 在 `Install dependencies` 步骤失败

**搁置日期**: 2026-07-09
**解决日期**: 2026-07-09 21:38
**最后状态**: ✅ **RESOLVED**

根因（详见 [commit `e90f49f`](https://github.com/liwt2010/all-agents/commit/e90f49f)）：
`requirements.txt` 用 PowerShell `pip freeze > requirements.txt` 生成时
stderr 泄漏到了文件末尾，pip 解析 index 时立刻 fail。修复 = 剥掉
PowerShell 泄漏 + 移除 `pywin32` + 移除冗余的 build-tool pins。

CI 状态：Run #22 (e90f49f) PASSED；Run #24/#25 PASSED。

---

## DEFERRED-002: WebSocket TestClient 框架限制（v0.3.0）

**搁置日期**: 2026-07-22
**状态**: ⚠️ Known limitation — not blocking

`TestClient.websocket_connect` 在 anyio 4.x + httpx 0.28 组合下返回
close code 1008，跨 starlette 0.36-1.3.1 + fastapi 0.111-0.138 全部复现。
是上游框架 bug，项目代码侧无解。

**绕行**: endpoint 在真实 uvicorn 下工作正常（用 `websockets` 客户端
验证）。Router-level `stream_chunks()` 测试通过；endpoint-level WS 测试
在 `tests/test_llm_stream.py` 用 `_ws_disabled` fixture 跳过并注明原因。

**重启时机**: 上游 starlette / anyio / httpx 修复 transport 后再启用。
跟踪链接: https://github.com/encode/starlette/issues (搜 WebSocket TestClient)

---

## DEFERRED-003: `openapi-python-client` 0.26 UP007 bug

**搁置日期**: 2026-07-09
**状态**: ⚠️ Upstream bug — not blocking

工具生成的 client 代码包含嵌套 `Union[IO[bytes], bytes, str]`，
它自带的 ruff 不能自动修复（UP007 fails on 2/690 sites）。

**绕行**: `tests/test_openapi_sdk.py` 中两个 SDK 生成测试标记为
`pytest.xfail`，等工具上游修复后取消 xfail。

**重启时机**: `openapi-python-client` 发布修复版本。

---

## 历史

- 2026-07-09: DEFERRED-001 filed (CI install step failure).
- 2026-07-09 21:38: DEFERRED-001 resolved (PowerShell stderr root cause).
- 2026-07-22: DEFERRED-002 + DEFERRED-003 added during v0.3.0 audit.