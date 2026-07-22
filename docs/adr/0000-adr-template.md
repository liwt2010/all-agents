# ADR-0000: Architectural Decision Records (this template)

**Status**: Accepted
**Date**: 2026-07-22
**Deciders**: Platform team

## Context

We need a lightweight, durable way to record significant
architectural decisions so future contributors can understand
*why* the code looks the way it does — not just *what* it does.

Lightweight because we don't want ceremony to slow down decisions;
durable because the reasoning often matters more than the outcome
once the original authors are gone.

## Decision

We adopt Michael Nygard's ADR format, with these conventions:

- Each ADR is a single Markdown file in `docs/adr/`.
- Filenames are zero-padded sequential: `NNNN-title-with-hyphens.md`.
  Existing ADRs use 4-digit padding; we keep that.
- Each ADR has these sections, in this order:
  1. **Status** — one of `Proposed`, `Accepted`, `Superseded by ADR-NNNN`,
     `Deprecated`. Status changes are appended at the bottom of the
     file (we do NOT rewrite history).
  2. **Date** — the date the decision was made (or last amended).
  3. **Deciders** — who was involved.
  4. **Context** — the forces at play, the problem we're solving.
  5. **Decision** — what we chose, in active voice.
  6. **Alternatives considered** — what we rejected, briefly, and why.
  7. **Consequences** — trade-offs; both positive (what becomes easier)
     and negative (what becomes harder).
- ADR-0000 (this file) defines the template itself.

## Consequences

Positive:
- Decisions stay close to the code (docs/adr is a sibling of src/).
- Reading an ADR explains "why" in a way that git log + code can't.
- Status field keeps the historical record durable without forcing
  rewrites.

Negative:
- ADRs can drift out of sync with code if not maintained.
- Reading the full set requires scrolling the directory listing.
  Mitigated by `docs/adr/README.md` (table of contents).

---

## Template (copy when writing a new ADR)

```markdown
# ADR-NNNN: <short title>

**Status**: Proposed
**Date**: YYYY-MM-DD
**Deciders**: <who>

## Context

<What is the issue we're seeing? What are the forces at play?
What constraints do we have?>

## Decision

<What we chose. Active voice. One sentence to one paragraph.>

## Alternatives considered

### <Alternative A>
<What it was, why we rejected it.>

### <Alternative B>
<What it was, why we rejected it.>

## Consequences

### Positive
- <What becomes easier.>

### Negative
- <What becomes harder or remains a risk.>
```

When the decision is later superseded, append a section at the end:

```markdown
## Status change

**Date**: YYYY-MM-DD
**Superseded by ADR-MMMN**: <one-line reason>.
```