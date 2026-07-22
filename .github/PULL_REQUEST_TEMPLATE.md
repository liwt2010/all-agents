<!--
Thanks for contributing! This template collects the metadata we
need to land your PR quickly. CI runs lint, type-check, the full
pytest suite, and the production-readiness gate on every push.
Read [CONTRIBUTING.md](../blob/main/CONTRIBUTING.md) for the
full workflow.
-->

## Summary

<!-- One paragraph. What does this PR change and why? -->

## Related

<!-- Link the issue, ADR, or roadmap item this closes. -->

- Closes #
- ADR: [docs/adr/0000-adr-template.md](../blob/main/docs/adr/0000-adr-template.md) (if introducing a new decision)
- Roadmap: link to TODO.md item (if addressing backlog)

## Test plan

- [ ] `pytest tests/ -q --ignore=tests/test_*real_llm.py` passes locally
- [ ] New tests cover the new behavior (if applicable)
- [ ] If the change is user-facing, docs/ + README.md updated
- [ ] If the change is a new env var, .env.example updated
- [ ] If the change touches a public API, CHANGELOG.md `[Unreleased]` updated

## Risk

<!-- What could break? How did you verify it doesn't? -->

- [ ] No new external dependency
- [ ] No DB schema change (or migration added)
- [ ] Backward-compatible (or breaking change called out in CHANGELOG)
- [ ] No new env var without default

## Notes for reviewer

<!-- Anything that needs explanation, screenshots, follow-ups. -->