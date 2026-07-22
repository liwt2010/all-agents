## Summary

- **New PEER path**: `AutoGenGroupChat` creates a `RoundRobinGroupChat` among the failing agent and up to 4 peer agents (Product, Tech, Test, Deploy, CEO), each with their own LLM configuration from `settings.yaml`. Gracefully falls back to the old `DiscussionMixin.discuss()` when AutoGen is not installed.
- **Multi-provider LLM Router**: Now supports both Anthropic (`AsyncAnthropic`) and OpenAI-compatible backends (DeepSeek, etc.) via `LLM_PROVIDER`/`OPENAI_API_KEY`/`OPENAI_BASE_URL` env vars.
- **Android lip-reading app** generated as a demo: `projects/lip-reading-android/` (standalone repo at liwt2010/lip-reading-android).
- Various production hardening (JWT auth fix in API client, `.env` loading, settings updates).

## Key files changed

| File | Change |
|------|--------|
| `src/agent_system/core/autogen_discussion.py` | **NEW** - AutoGen 0.4+ RoundRobinGroupChat PEER implementation |
| `src/agent_system/core/resolver.py` | _PeerDiscussionAdapter rewritten: AutoGen first, DiscussionMixin fallback |
| `src/agent_system/core/llm_router.py` | Multi-provider: Anthropic + OpenAI-compatible (DeepSeek) |
| `src/agent_system/config/settings.yaml` | Models changed to deepseek-chat |
| `src/agent_system/api/server.py` | load_dotenv() on startup |
| `web/src/lib/api.ts` | JWT auth interceptors |
| `pyproject.toml` | autogen extra: pyautogen -> autogen-agentchat |
| `Dockerfile`, `docker-compose.yml`, `.env.example` | Updated dependencies |

## Testing

```
tests/test_discussion_mixin.py .........    9 passed
tests/test_resolver_peer_integration.py ..  2 passed
tests/test_iteration4.py ................. 19 passed
                                     Total: 30 passed
```

## How to test PEER path

1. Install AutoGen: `pip install autogen-agentchat autogen-ext[openai]`
2. Set env: `LLM_PROVIDER=openai OPENAI_API_KEY=sk-...`
3. Start server, submit a task, then inject a failure to trigger SmartResolver which will select ResolutionPath.PEER and launch the GroupChat.

## Notes

- The `autogen` dependency is optional (extras) - without it the old DiscussionMixin path runs.
- The Android project `projects/lip-reading-android/` is a separate repository (liwt2010/lip-reading-android) and is NOT tracked by this repo.
- Temp files (`tmp_*.py`) and build artifacts should be cleaned up after review.
