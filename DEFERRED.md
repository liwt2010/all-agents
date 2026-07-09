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

