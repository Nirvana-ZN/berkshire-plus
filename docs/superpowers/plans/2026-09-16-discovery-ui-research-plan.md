# Discovery UI and Research Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the guided “每周发现” Vue workflow, case-card adjudication, weekly deep-research handoff, and safe retirement of the old cheap-board primary entry.

**Architecture:** Add one monolithic discovery view using existing table/polling/form patterns; avoid Pinia and new UI dependencies. Extend the existing research job path with discovery provenance and a server-side three-company weekly quota.

**Tech Stack:** Vue 3, TypeScript, Vue Router, Vite, FastAPI, SQLAlchemy 2, pytest.

---

## File map

- Modify: `app/models.py` — already gains research provenance in Plan 2.
- Modify: `app/db.py` — unique discovery research index.
- Modify: `app/discovery_routes.py` — research start endpoint and quota.
- Modify: `jobs/research.py` — accept discovery provenance without changing existing callers.
- Modify: `app/main.py` — typed job payload/link compatibility if needed.
- Create: `tests/test_discovery_research.py`
- Create: `web/src/views/DiscoveryView.vue`
- Modify: `web/src/router/index.ts`
- Modify: `web/src/App.vue`
- Modify: `web/src/views/HomeView.vue`
- Modify: `web/src/views/ResearchView.vue` — show optional source only.
- Modify: `tests/test_discovery.py`
- Modify: `tests/test_health.py`
- Modify: `tests/test_cheap.py` — final redirect/deprecation only.

## Task 1: Add idempotent research provenance and quota

**Files:**
- Modify: `app/db.py`
- Modify: `app/discovery_routes.py`
- Modify: `jobs/research.py`
- Create: `tests/test_discovery_research.py`

- [ ] **Step 1: Write failing research handoff tests**

Cover:

- only a final kept candidate may start discovery research;
- created `ResearchJob` stores `discovery_run_id`, `discovery_track`, and existing `code`;
- same `(run, code)` request returns the existing job and does not consume quota twice;
- concurrent same-code requests from two sessions return one job;
- a company appearing in two tracks consumes one weekly slot;
- fourth distinct company in the same user/week is rejected;
- concurrent third/fourth-code requests cannot both pass the quota;
- canceled/failed jobs do not release an accepted slot;
- normal `/api/screens/{id}/research` jobs keep discovery fields null;
- no candidate is added to watchlist before research verdict allows it.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_research.py -q
```

Expected: FAIL because the discovery research endpoint does not exist.

- [ ] **Step 3: Add database idempotency**

Create:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_research_discovery_candidate
ON research_jobs(discovery_run_id, code)
WHERE discovery_run_id IS NOT NULL
```

Quota is computed by joining `ResearchJob.discovery_run_id` to `DiscoveryRun` and counting distinct accepted codes for `(user_id, week_key)`. Catch the unique-index `IntegrityError`, roll back, and return the existing job.

- [ ] **Step 4: Add the typed endpoint**

```text
POST /api/discoveries/{id}/research
body: {"track": "quality_discount", "code": "600519.SH"}
```

Validate the persisted final decision/evidence and abandoned state. Acquire the existing global heavy-job lock with a temporary `discovery_research_accept` reservation keyed by run ID before quota count and insertion, in that order, so concurrent requests cannot overrun the count. After insert, atomically replace the reservation with the new research job ID/type before dispatch. On failure release the reservation in `finally`. If another heavy job owns it, return 409 before consuming a slot.

- [ ] **Step 5: Reuse research execution**

Pass optional provenance to the existing creation helper. Do not fork the research evaluator.

- [ ] **Step 6: Run and commit**

```powershell
uv run pytest tests/test_discovery_research.py tests/test_research.py tests/test_screen.py -q
git add app/db.py app/discovery_routes.py jobs/research.py tests/test_discovery_research.py
git commit -m "feat: launch research from discovery candidates"
```

## Task 2: Add the route and empty discovery shell

**Files:**
- Create: `web/src/views/DiscoveryView.vue`
- Modify: `web/src/router/index.ts`
- Modify: `web/src/App.vue`
- Modify: `web/src/views/HomeView.vue`
- Modify: `tests/test_discovery.py`

- [ ] **Step 1: Write the failing frontend contract test**

```python
def test_views_show_weekly_discovery() -> None:
    app = (ROOT / "web/src/App.vue").read_text(encoding="utf-8")
    home = (ROOT / "web/src/views/HomeView.vue").read_text(encoding="utf-8")
    router = (ROOT / "web/src/router/index.ts").read_text(encoding="utf-8")
    page = (ROOT / "web/src/views/DiscoveryView.vue").read_text(encoding="utf-8")
    assert 'to="/discover"' in app
    assert 'to="/discover"' in home
    assert "DiscoveryView" in router
    assert "/api/discoveries" in page
    assert "每周发现" in page
    assert "买入" not in page
    assert "<pre" not in page.lower()
```

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery.py::test_views_show_weekly_discovery -q
```

Expected: FAIL because `DiscoveryView.vue` is absent.

- [ ] **Step 3: Add route/nav/home entry**

Add `/discover` before `/cheap`. Keep `/cheap` visible as `低估值（旧版）` until Task 6.

- [ ] **Step 4: Implement empty/loading/error states**

On mount:

```text
GET /api/discoveries/current
```

Treat 404 as “本周尚未创建批次”; other errors show `detail || error`. “开始本周发现” calls `POST /api/discoveries`.

- [ ] **Step 5: Type-check and commit**

```powershell
cd web
npm run type-check
cd ..
uv run pytest tests/test_discovery.py::test_views_show_weekly_discovery -q
git add web/src/views/DiscoveryView.vue web/src/router/index.ts web/src/App.vue web/src/views/HomeView.vue tests/test_discovery.py
git commit -m "feat: add weekly discovery entry"
```

## Task 3: Render guided stages and confirmations

**Files:**
- Modify: `web/src/views/DiscoveryView.vue`
- Modify: `tests/test_discovery.py`

- [ ] **Step 1: Write failing source-contract assertions**

Assert the page contains:

- 共同排雷、优质折价、深度价值、合理价成长;
- 轨道门槛、候选竞争、人工裁决;
- 通过、淘汰、数据不足、人工覆盖;
- 本层固定规则、尺子版本/改因、计数、指标分布、阈值位置;
- `waiting_confirmation`, `failed`, `data_not_ready`, `ruler_not_ready`;
- safety and typed track-stage API paths;
- no raw `snapshot_json` rendering.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery.py -q
```

- [ ] **Step 3: Add in-view types and state**

Keep interfaces in `DiscoveryView.vue`, matching existing views:

```typescript
interface DiscoveryRow {
  code: string
  name: string
  verdict: 'pass' | 'drop' | 'insufficient'
  reason: string | null
  rank: number | null
  metrics: Record<string, string | number | null>
}
```

Use `activeRunId` and `activeStageKey` guards so stale poll responses cannot replace a newly selected stage.

- [ ] **Step 4: Reuse polling and table patterns**

- Poll every 800ms only while a stage is queued/running.
- Stop on waiting/terminal status and unmount.
- Render four structured tables, not JSON.
- Render `summary.rules`, ruler version/reason, counts, metric quantiles, and threshold markers above the tables; show unavailable rather than inventing a distribution.
- Show confirm only in `waiting_confirmation`.
- Show retry only in `failed`.
- Keep data/ruler-not-ready read-only with abandon/recreate guidance.

- [ ] **Step 5: Add override form**

Require non-empty reason before POST `/overrides`. Refresh the current stage after success.

- [ ] **Step 6: Verify and commit**

```powershell
cd web
npm run type-check
cd ..
uv run pytest tests/test_discovery.py -q
git add web/src/views/DiscoveryView.vue tests/test_discovery.py
git commit -m "feat: guide discovery stage confirmations"
```

## Task 4: Add case cards, adjudication, and research action

**Files:**
- Modify: `web/src/views/DiscoveryView.vue`
- Modify: `web/src/views/ResearchView.vue`
- Modify: `app/main.py`
- Modify: `tests/test_discovery.py`
- Modify: `tests/test_discovery_research.py`

- [ ] **Step 1: Write failing UI assertions**

Require each case card to show:

- track;
- why it surfaced;
- 3–5 key evidence rows;
- at least one risk or `尚无结构化反方证据`;
- data gaps/freshness;
- weekly change;
- only `值得深研 / 观察 / 排除`.

Assert the actions are `保留为候选`, `排除`, and `加入本周深研`; exclude requires a reason.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery.py tests/test_discovery_research.py -q
```

- [ ] **Step 3: Render cards with existing panel/table CSS**

Do not create a component until markup is duplicated in three places. One loop in the view is sufficient.

- [ ] **Step 4: Wire decisions and completion**

Post decisions, show live keeper count, and allow explicit `候选不足` completion for 0–2 kept candidates. Do not visually claim success below three.

- [ ] **Step 5: Wire research**

POST the typed discovery endpoint, then:

```typescript
router.push(`/research/${data.id}`)
```

Show server quota errors unchanged. Add an optional “来源：每周发现 / 轨道” badge to `ResearchView.vue`; existing research jobs render as before.

Extend `app.main._artifact_payload()` to return nullable `discovery_run_id` and `discovery_track`. Add API compatibility tests proving ordinary research payloads contain null provenance and discovery-launched jobs contain both values.

- [ ] **Step 6: Verify and commit**

```powershell
cd web
npm run type-check
cd ..
uv run pytest tests/test_discovery.py tests/test_discovery_research.py tests/test_research.py -q
git add web/src/views/DiscoveryView.vue web/src/views/ResearchView.vue app/main.py tests/test_discovery.py tests/test_discovery_research.py
git commit -m "feat: adjudicate discovery case cards"
```

## Task 5: Show weekly changes and home progress

**Files:**
- Modify: `web/src/views/DiscoveryView.vue`
- Modify: `web/src/views/HomeView.vue`
- Modify: `app/main.py`
- Modify: `tests/test_discovery.py`

- [ ] **Step 1: Write failing display tests**

Assert the page maps:

```text
data_changed -> 数据变化
rule_changed -> 尺子变化
both_changed -> 数据与尺子同时变化
decision_changed -> 人工裁决变化
```

Home must link discovery jobs to `/discover` and show the current batch status.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery.py -q
```

- [ ] **Step 3: Render compact change rows**

Show name/code, prior state, current state, and cause. Do not claim a single cause for `both_changed`.

- [ ] **Step 4: Extend home/job payload**

Add `discovery: "每周发现"` to label/link maps and reuse the existing 3000ms workbench refresh. Do not add a second timer.

- [ ] **Step 5: Build and run tests**

```powershell
cd web
npm run type-check
npm run build-only
cd ..
uv run pytest tests/test_discovery.py tests/test_health.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add web/src/views/DiscoveryView.vue web/src/views/HomeView.vue app/main.py tests/test_discovery.py
git commit -m "feat: show weekly discovery changes"
```

## Task 6: Demote the legacy cheap board after acceptance

**Files:**
- Modify: `web/src/router/index.ts`
- Modify: `web/src/App.vue`
- Modify: `web/src/views/HomeView.vue`
- Modify: `tests/test_cheap.py`
- Modify: `tests/test_health.py`

- [ ] **Step 1: Run the end-to-end acceptance checklist manually**

Using one seeded/sync test run or local data:

1. create weekly run;
2. confirm common safety;
3. complete all three track gates;
4. inspect competition exclusions;
5. make 3–5 decisions per successful track;
6. start one research job;
7. reload and verify frozen results;
8. verify prior-week changes.

Do not continue if any step loses state or bypasses confirmation.

- [ ] **Step 2: Write redirect/deprecation test**

Choose one approved behavior:

- retain `/cheap` but remove it from primary nav; or
- redirect `/cheap` to `/discover`.

The design permits either. Prefer nav demotion first because it preserves rollback access.

- [ ] **Step 3: Update navigation copy**

Make `每周发现` the primary full-market entry and label cheap board as old/secondary only where retained.

- [ ] **Step 4: Final verification**

```powershell
cd web
npm run type-check
npm run build-only
cd ..
uv run pytest -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add web/src/router/index.ts web/src/App.vue web/src/views/HomeView.vue tests/test_cheap.py tests/test_health.py
git commit -m "chore: make weekly discovery the primary entry"
```

## Plan 4 acceptance

- The user can pause and confirm every layer from one page.
- Tables/cards explain results without raw JSON.
- Discovery never emits buy/sell language.
- Research handoff preserves run/track/code and enforces three distinct companies per week.
- Existing research and watchlist flows remain compatible.
- `/cheap` is not removed until the new flow passes end-to-end acceptance.
