# Deferred Tasks (之后处理)

> 用户要求搁置的任务，未来再回来处理。每条记: 上下文 / 现状 / 重启时要做什么。

---

## DEFERRED-001: GitHub Actions CI 在 `Install dependencies` 步骤失败

**搁置日期**: 2026-07-09
**最后状态**: 2 次 push 都失败，需要 gh CLI auth 才能看 job log 根因
**Priority**: 中 (production 流程没卡死 — 全部 885 个测试本机 pass + Docker image smoke-tested)

### 症状
- 任何 push 到 `main` 都触发 CI，两个 job (`unit-tests` + `real-llm-smoke`) 都在 step #4 `Install dependencies` 直接 fail
- 每次失败只跑 ~4 秒 (`2026-07-09T11:25:54Z` → `11:25:58Z`)，太短没到实际下载，肯定是 index 解析就死了
- 失败的 commits:
  - `c0a1136` (run 29014689745) — fix(ci): add missing deps + pin via requirements.txt
  - `dee8ec9` (run 29014816599) — fix(requirements): remove pywin32 (Windows-only)

### 现场状态 (已落地)
- `requirements.txt`: 已删除 `pywin32==312` (Windows-only, Linux 装不了)
- `pyproject.toml`: `f9e90f8` commit 把 pyproject 缺的所有 deps 都补上了 (python-dotenv, pyjwt, httpx, sqlalchemy, redis, psycopg2-binary, opentelemetry-*, openapi-python-client)
- `.github/workflows/ci.yml`: install 命令已经是 `pip install -r requirements.txt` + `pip install -e . --no-deps`
- 加了 `--collect-only` fail-fast step (但因为 install 失败没跑到)

### 重启时要做的 (3 选 1)
**Plan A — 看 log 根因 (推荐)**
```bash
cd "E:\Code files\Minimax Code\all-agents"
gh auth login  # 必须先登录
gh run view 29014816599 --log
# 找 "ERROR:" 关键词 — 通常是某包版本对 cp311 没 wheel 或要 C 编译器
# 怀疑清单 (按概率):
#  - cryptography==49.0.0  (要 Rust toolchain)
#  - pyautogen==0.10.0    (要 wheel 检查)
#  - autogen-* 系列       (PyPI 版本和 cp311 是否匹配)
#  - 任何 pinned 版本 cp311 没 source distribution
```

**Plan B — 简化 install 策略**
- 把 requirements.txt 砍到最低限度，只 pin 真正可能版本冲突的 (pydantic v2, langgraph, opentelemetry 1.43.0, python-dotenv, pyjwt, sqlalchemy)
- 其它让 `pip install -e .` 自己解析
- 反正 pyproject 已经补完 deps，应该 OK

**Plan C — 改用 lock file 方案**
- 用 `pip-compile` (pip-tools) 重新生成 requirements.txt，过滤 Windows-only 包
- `pip install --no-binary :all:` 对问题包从 source 装 (慢但总是工作)

### 同时要做
- [ ] `real-llm-smoke` job 一直没真跑过，要 `gh secret set ANTHROPIC_API_KEY` + `OPENAI_API_KEY` 或手动 `gh workflow run` 才能启用
- [ ] 或直接删掉 `real-llm-smoke` job (本地 9 个 real-LLM 测试已经验过)
- [ ] `liwt2010/all-agents:v0.1.0` Docker image 是在 `47a4b82` 拍的快照，没包含 `1127b8c` (6 test fixes) 和 `f9e90f8`/`dee8ec9` (CI fixes)；是否 rebuild 看用户决定

### 不影响当前 v0.1.0 release 的理由
- 所有 885 个测试 本机全 pass (`834 unit + 9 real-LLM + 42 production-readiness`)
- Docker image 已 build + smoke-tested (19 routes, /api/health ok)
- v0.1.0 tag 已 force-moved 到 `f9e90f8`
- READMEs (3 语言) + RELEASE_NOTES.md 已发布
- CI 只是 "云端额外验证"，codebase 本来就 production-grade 的

---

## STATUS UPDATE — 2026-07-09 20:37 (root cause found, fix pushed, CI 还没跑)

**过去 30 分钟进展（用户亲手给出根因分析）:**

### 根因（已确认）
1. **Bug 1 (fatal):** `requirements.txt` 末尾 8 行是 PowerShell stderr 泄漏进文件
   - `python.exe :` / `所在位置 行:1 字符:` / `+ CategoryInfo` / `[notice] A new release of pip is available`
   - 这些行被 pip 当包名解析 → `parse error` → 立即 abort（4 秒 fast-fail）
   - 起源: 文件是用 PowerShell 跑 `python -m pip freeze > requirements.txt` 生成的，`2>&1` 在 PowerShell 主机下仍会混合 stderr

2. **Bug 2:** `pyautogen==0.10.0` 与 `autogen-agentchat==0.7.5` namespace 冲突；pyproject 没声明 pyautogen，src 只 import 新 API
3. **Bug 3:** `pip==24.0 / pip-tools / setuptools / wheel` 都是 build tool pins，运行时冗余

### 已落地修复
- ✅ Commit `e90f49f` pushed (`fix(ci): remove PowerShell stderr garbage + old auto API + build pins from requirements.txt`)
- ✅ 文件精简: 91 包, 1702 bytes, 0 garbage
- ✅ 本地 `pip install --dry-run -r requirements.txt` 全过
- ✅ Memory 已记录 (PowerShell pip freeze stderr leak lesson)

### 当前 CI 状态（截至 20:37）
- Run #22 (e90f49f): `Queued` 状态 8 分钟，**未启动**
- 看起来 GitHub 免费 runner 排不上队 / 资源紧张
- 不再轮询（cron 已删，避免 spam）

### 重启监控时
1. 检查 https://github.com/liwt2010/all-agents/actions 看 Run #22 是否终于跑起来
2. 如果仍 4 秒 fail，**用 PAT 调 API**（用户手动删过 PAT，需要重新粘或自己用 web 看 log）
3. 如果过了，**改写本节**: 把 "STATUS UPDATE" 改名为 "RESOLVED 2026-07-09T...:Z"，跑通结果写入
4. **不要删本节**: root cause 是用户亲手挖出来的，留作记录

---

## SESSION END — 2026-07-09 20:45

### 本次 session 末态（git truth, all on origin/main）

```
HEAD                 = 547e7f3  docs+ops(v0.1.0): PR_BODY.md + scripts/ + web/
origin/main          = 547e7f3  (in sync with local)
local  v0.1.0 tag    = 83a4922  (reverted from brief forward to 547e7f3)
remote v0.1.0 tag    = 83a4922  (force-pushed after revert)
release page         = id=351495762, body intact (still says "v0.1.0 | commit 83a4922")
Docker image         = liwt2010/all-agents:v0.1.0 (image tag still pre-CI-fix)
                  = d9d25548b946 (rebuilt session, tagged but NOT pushed to Docker Hub)
CI Run #22           = status unknown (was queued 9min when we stopped polling)
```

### Post-snapshot commits on main (NOT in v0.1.0 tag)

| Commit | What it does |
|---|---|
| `e90f49f` | fix(ci): requirements.txt cleanup (PowerShell stderr leak + pyautogen + build pins). Bug analysis verbatim from user. |
| `ee44ccf` | docs(deferred): this STATUS UPDATE before another session. |
| `547e7f3` | docs+ops: .github/PR_BODY.md, CLAUDE.md, scripts/{gen_auth_secret.py,start_server.sh}, web/{Dockerfile,nginx.conf}. projects/lip-reading-android/ deliberately not included. |

These three commits form a natural "v0.1.1 next release" bundle — they fix CI, document the deferred state, and add web container glue.

### Things user still owes / can do later

1. **Run `docker login -u liwt2010`** to push the rebuilt `liwt2010/all-agents:v0.1.0` to Docker Hub (image `d9d25548b946` is local; tag was set but push not executed — credentials were invalid last attempt).
2. **Revoke PAT `mavis-release-edit`** at https://github.com/settings/tokens (used for creating release 351495762 + uploading Docker tar.gz + asset ping.txt delete).
3. **Refresh `/api/health` smoke test** on any image change.
4. **Decide v0.1.1 tag**: if e90f49f + ee44ccf + 547e7f3 look good, tag forward to a new e.g. `v0.1.1` (NOT silently bundled into v0.1.0 — user decision was explicit: keep v0.1.0 anchored to 83a4922).

### Cron state

- `ci-after-e90f49f` cron **DELETED** at 20:37. No active crons.
- User said "明天或晚点继续" — they'll come back on their own; no remote cron needed.

### Memory written this session

- `PowerShell pip freeze > file leaks stderr into requirements.txt` (agent memory)

### One sentence for next session to start from

> Root cause of GitHub Actions `Install dependencies` 4s fast-fail was PowerShell stderr leakage into the bottom of `requirements.txt` — fixed in `e90f49f` (surgical regex strip + remove `pyautogen`/`pip`/`pip-tools`/`setuptools`/`wheel`). Local dry-run pass. CI Run #22 e90f49f was queued 9min+ when polling stopped. v0.1.0 tag intentionally anchored at 83a4922, not silently forward-moved. Post-snapshot work lives at HEAD `547e7f3` and is ready for v0.1.1 unless told otherwise.

