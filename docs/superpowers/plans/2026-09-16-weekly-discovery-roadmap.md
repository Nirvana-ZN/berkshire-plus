# Weekly Discovery Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved weekly three-track discovery workflow through four independently testable plans.

**Architecture:** Build data credibility first, then the persisted guided workflow, then three independent rule tracks, and finally the Vue/research integration. The legacy `/cheap` board remains available until the new flow is verified end-to-end.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, SQLite, httpx2, pytest, Vue 3, TypeScript, Vite.

---

## Plan order

1. [`2026-09-16-discovery-data-plan.md`](2026-09-16-discovery-data-plan.md)
   Verify East Money sources, add historical valuation and required financial fields, then deliver resumable backfill and readiness reporting.

2. [`2026-09-16-discovery-framework-plan.md`](2026-09-16-discovery-framework-plan.md)
   Add frozen weekly runs, common safety, staged confirmation, retries, abandonment, overrides, decisions, and read APIs.

3. [`2026-09-16-discovery-tracks-plan.md`](2026-09-16-discovery-tracks-plan.md)
   Add versioned rulers, quality-discount/deep-value/GARP gates, deterministic competition, evidence cards, and weekly change classification.

4. [`2026-09-16-discovery-ui-research-plan.md`](2026-09-16-discovery-ui-research-plan.md)
   Add the guided Vue page, research provenance/quota, navigation, and eventual `/cheap` deprecation.

## Hard gates

- Plan 1 must prove a source and field dictionary before any rule consumes the new data.
- Plan 2 may use synthetic fixture data, but no track is activated before Plan 1 readiness passes.
- Plan 3 activates one track at a time; quality discount is the first vertical slice.
- Plan 4 keeps `/cheap` until all three tracks and the research handoff pass acceptance checks.

## Definition of done

- Each plan's focused tests pass.
- `uv run pytest -q` passes after every plan.
- Frontend plans additionally pass `npm run type-check` and `npm run build-only`.
- Each task is committed separately using only the files listed in that task.
