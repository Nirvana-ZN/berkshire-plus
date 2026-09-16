# Guided Discovery Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver frozen weekly discovery runs with common safety, independent track stages, confirmation gates, retries, abandonment, overrides, decisions, and replayable results.

**Architecture:** Keep legacy `ScreenRun` untouched. Add discovery-specific ORM models and a pure state machine, then place SQLAlchemy orchestration in focused service modules and expose thin FastAPI routes.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, SQLite, pytest.

---

## File map

- Create: `app/discovery_models.py` — discovery ORM tables only.
- Modify: `app/db.py` — import models, create partial unique index, additive research columns.
- Create: `discovery/constants.py` — tracks, stages, statuses, verdicts.
- Create: `discovery/aggregate.py` — run-status precedence.
- Create: `discovery/builds.py` — executable algorithm-build registry.
- Create: `discovery/snapshot.py` — immutable input freeze/read.
- Create: `discovery/workflow.py` — transitions, confirmation, retry, abandonment.
- Create: `discovery/common.py` — shared status/data-completeness classification.
- Create: `discovery/overrides.py` — effective stage verdicts.
- Create: `jobs/discovery.py` — compute/persist background jobs.
- Create: `app/discovery_routes.py` — discovery API router.
- Modify: `app/main.py` — include router and start background jobs through app state.
- Modify: `jobs/lock.py` — discovery job type and stale cleanup.
- Create: `tests/test_discovery_workflow.py`
- Create: `tests/test_discovery_common.py`
- Create: `tests/test_discovery_overrides.py`
- Create: `tests/discovery_helpers.py`

## Task 1: Add discovery persistence

**Files:**
- Create: `app/discovery_models.py`
- Modify: `app/db.py`
- Modify: `app/models.py`
- Create: `tests/test_discovery_workflow.py`

- [ ] **Step 1: Write failing schema tests**

Assert all nine tables exist:

```python
expected = {
    "discovery_runs",
    "discovery_input_snapshots",
    "discovery_common_stages",
    "discovery_common_results",
    "discovery_stages",
    "discovery_results",
    "discovery_overrides",
    "discovery_decisions",
    "discovery_evidence",
}
```

Also test:

- only one `current=true` run per `(user_id, week_key)`;
- multiple abandoned `current=false` runs are allowed;
- duplicate `(run, track, stage, attempt, code)` results are rejected;
- duplicate common/track attempts, overrides, decisions, and evidence keys are rejected;
- `ResearchJob` accepts nullable `discovery_run_id` and `discovery_track`.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_workflow.py -q
```

Expected: FAIL because discovery tables do not exist.

- [ ] **Step 3: Add focused ORM models**

Keep these in `app/discovery_models.py`, imported by `app/db.py::init_db` before `Base.metadata.create_all`.

Use string columns for statuses/tracks to match existing models. Add unique constraints for attempts/results and foreign keys to `users`, `discovery_runs`, and `research_jobs` where applicable.

`DiscoveryRun` has nullable `replay_of_run_id` for a recomputation that copies an old immutable snapshot into a new run.

Stage rows also store `input_fingerprint`, `ruler_version_id`, `algorithm_version`, nullable `failure_kind` (`transient` or `permanent`), and nullable `superseded_at` so corrected downstream attempts remain replayable. Required uniqueness:

```text
common stage: (run_id, attempt)
track stage: (run_id, track, stage, attempt)
common result: (run_id, attempt, code)
track result: (run_id, track, stage, attempt, code)
override: (run_id, track, stage, attempt, code)
decision: (run_id, track, competition_attempt, code)
evidence: (run_id, track, competition_attempt, code, kind, rule_id)
```

Create the partial index with raw SQL:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_discovery_current_week
ON discovery_runs(user_id, week_key)
WHERE current = 1
```

Do not use `UniqueConstraint(user_id, week_key, current)`: it would permit only one abandoned row.

- [ ] **Step 4: Add research provenance columns**

Add nullable columns to `ResearchJob` and `_ensure_research_columns()`:

```python
discovery_run_id: Mapped[int | None]
discovery_track: Mapped[str | None]
```

- [ ] **Step 5: Run schema tests**

```powershell
uv run pytest tests/test_discovery_workflow.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add app/discovery_models.py app/models.py app/db.py tests/test_discovery_workflow.py
git commit -m "feat: add discovery persistence"
```

## Task 2: Implement the pure state machine

**Files:**
- Create: `discovery/constants.py`
- Create: `discovery/aggregate.py`
- Create: `discovery/builds.py`
- Create: `discovery/workflow.py`
- Modify: `tests/test_discovery_workflow.py`

- [ ] **Step 1: Write transition-table tests**

Test exact normal flow:

```text
queued -> running -> waiting_confirmation -> confirmed
```

Test:

- only `failed` may retry with unchanged inputs;
- only `failure_kind="transient"` may retry;
- retry creates `attempt+1` and preserves old rows;
- `data_not_ready`, `ruler_not_ready`, and `insufficient_candidates` reject retry;
- confirming twice is idempotent;
- competition must confirm before adjudication;
- abandoned runs reject all mutations;
- aggregate precedence is `abandoned`, `failed`, `completed`, `partially_completed`, `running`, `waiting_confirmation`, `queued`.
- unavailable historical build rejects recomputation with `algorithm_unavailable`, while persisted history remains readable.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_workflow.py -q
```

Expected: FAIL on missing transition helpers.

- [ ] **Step 3: Implement constants and pure validation**

Use frozen sets:

```python
TRACKS = frozenset({"quality_discount", "deep_value", "garp"})
TRACK_STAGES = ("track_gate", "competition", "adjudication")
TERMINAL_FAILURES = frozenset(
    {"failed", "ruler_not_ready", "data_not_ready", "insufficient_candidates"}
)
```

Keep transition validation pure; database writes belong in orchestration functions.

Add a small build registry:

```python
BUILD_VERSION = "discovery-v1"
AVAILABLE_BUILDS = {BUILD_VERSION}
```

Retry/recompute requires the same input fingerprint, ruler ID, and an available algorithm build. A caught network/process interruption is transient; invalid data/ruler/business invariants are permanent or their dedicated terminal state.

- [ ] **Step 4: Implement aggregate status**

Use explicit ordered conditions. A common-stage failure makes the run failed. `partially_completed` requires all tracks terminal, at least one confirmed, and not all confirmed.

- [ ] **Step 5: Run tests**

```powershell
uv run pytest tests/test_discovery_workflow.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add discovery/constants.py discovery/aggregate.py discovery/builds.py discovery/workflow.py tests/test_discovery_workflow.py
git commit -m "feat: define discovery stage workflow"
```

## Task 3: Freeze immutable weekly inputs

**Files:**
- Create: `discovery/snapshot.py`
- Create: `tests/discovery_helpers.py`
- Modify: `tests/test_discovery_workflow.py`

- [ ] **Step 1: Write failing snapshot tests**

Seed two companies, create a run, mutate `financials` and the universe cache, then assert the run still reads the original values.

Also test:

- frozen row stores field values, source/as-of, industry, security status, data versions, ruler payload IDs, and algorithm version;
- run metadata freezes the latest exchange trading dates used by the suspension rule;
- each frozen company stores its own canonical input fingerprint;
- recent listing and missing fields remain represented rather than dropped;
- replay reads persisted results/evidence and does not invoke the current rule engine.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_workflow.py -q
```

Expected: FAIL on missing snapshot builder.

- [ ] **Step 3: Implement one-row-per-company snapshots**

Use JSON for the frozen field payload but normalized rows for companies:

```python
def freeze_inputs(session, run, universe, financials, valuations, versions) -> None:
    for company in universe:
        session.add(
            DiscoveryInputSnapshot(
                run_id=run.id,
                code=company.code,
                name=company.name,
                industry=company.industry,
                status=company.status,
                fields_json=json.dumps(..., sort_keys=True),
                sources_json=json.dumps(..., sort_keys=True),
            )
        )
```

Do not retain ORM objects or read mutable caches during later stages.

- [ ] **Step 4: Add deterministic fingerprint**

Hash each company's canonical fields/sources/as-of into `DiscoveryInputSnapshot.input_fingerprint`. Hash the ordered company fingerprints + data/ruler/algorithm versions into the run fingerprint. Assert identical inputs produce identical fingerprints.

- [ ] **Step 5: Run tests and commit**

```powershell
uv run pytest tests/test_discovery_workflow.py -q
git add discovery/snapshot.py tests/discovery_helpers.py tests/test_discovery_workflow.py
git commit -m "feat: freeze weekly discovery inputs"
```

## Task 4: Implement common safety and persistence

**Files:**
- Create: `discovery/common.py`
- Create: `jobs/discovery.py`
- Create: `tests/test_discovery_common.py`
- Modify: `jobs/lock.py`

- [ ] **Step 1: Write failing common-safety tests**

Cover:

- ST/delisting/invalid code drops;
- a suspended security passes only when its frozen last valid trade date is within five exchange trading days;
- missing shared price/industry is insufficient;
- missing GARP-only revenue does not make quality/deep-value globally insufficient;
- financial-company debt/OCF rules do not run here;
- common results store pass/drop/insufficient with reasons.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_common.py -q
```

Expected: FAIL on missing classifier/executor.

- [ ] **Step 3: Implement a small deterministic classifier**

Return a dataclass, not ORM rows:

```python
@dataclass(frozen=True)
class CommonVerdict:
    code: str
    verdict: str
    reason: str | None
    metrics: dict[str, object]
```

- [ ] **Step 4: Add job execution**

`execute_common_stage(session, stage_id)` must:

1. atomically set queued→running;
2. read only `DiscoveryInputSnapshot`;
3. persist `DiscoveryCommonResult`;
4. set `waiting_confirmation`;
5. on exception set failed/error;
6. classify exceptions with `failure_kind`.

Add `drain_run_queue(run_id)`: while holding one global discovery lock keyed by the run ID, claim the next queued common/track stage in deterministic `(created_at, id)` order, execute it, and continue until no queued stages remain. Release the lock once in `finally`. On restart, only `running` discovery stages become transient `failed`; untouched `queued` stages remain queued and are drained when the run is resumed.

Add `JOB_TYPE_DISCOVERY` and startup stale-stage failure handling in `jobs/lock.py`.

- [ ] **Step 5: Run tests**

```powershell
uv run pytest tests/test_discovery_common.py tests/test_health.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add discovery/common.py jobs/discovery.py jobs/lock.py tests/test_discovery_common.py
git commit -m "feat: run discovery common safety"
```

## Task 5: Add create/current/common-stage APIs

**Files:**
- Create: `app/discovery_routes.py`
- Modify: `app/main.py`
- Modify: `tests/test_discovery_workflow.py`

- [ ] **Step 1: Write failing API tests**

Cover:

- `POST /api/discoveries` creates one current weekly run and freezes inputs;
- duplicate create returns the existing run or 409, never a duplicate;
- `GET /api/discoveries/current` returns aggregate status;
- common-stage GET partitions pass/drop/insufficient;
- generic track-stage GET returns latest/current attempt and preserved historical attempts;
- confirm is idempotent and creates exactly one track-gate stage per track;
- confirming common safety acquires the discovery lock before state mutation and one drain worker computes all three queued gates serially;
- with injected fake track executors, all three tracks independently traverse gate → competition → adjudication and pause after each computed layer;
- two-session create and confirm races produce one run, one confirmation, and three track stages;
- abandon atomically clears current and prevents mutations;
- sync mode works only with `allow_sync=True`.
- startup resumes queued stages when a process died after confirmation committed but before the worker started.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_workflow.py -q
```

Expected: 404 for all discovery endpoints.

- [ ] **Step 3: Add a thin router**

Create `create_discovery_router()` and include it in `create_app()`. Access clients/session factory through `request.app.state`, matching existing route behavior.

Allow `create_app()` tests to inject a deterministic track executor; production uses the registry added by Plan 3. Until then, missing real rulers end in `ruler_not_ready`, not fabricated results.

Implement the read route now:

```text
GET /api/discoveries/{id}/tracks/{track}/stages/{stage}
```

Do not add all routes directly to the already-large `app/main.py`.

- [ ] **Step 4: Implement transactional confirmation**

Use conditional update:

```python
result = session.execute(
    update(DiscoveryCommonStage)
    .where(
        DiscoveryCommonStage.id == stage_id,
        DiscoveryCommonStage.status == "waiting_confirmation",
    )
    .values(status="confirmed", confirmed_at=_now())
)
```

Acquire the global lock before the transaction that confirms/enqueues. If another heavy job owns it, return 409 without confirming. Only the request that updates one row may enqueue downstream stages and launch `drain_run_queue`; duplicate confirm returns current state without a second worker.

Add `resume_discovery_queues()` to application startup. It scans non-abandoned runs with queued stages, acquires the global lock for one run at a time, and launches the same drain function. Test a database containing confirmed upstream + queued downstream + free lock before creating `TestClient`; startup must compute that queue exactly once.

- [ ] **Step 5: Run tests and commit**

```powershell
uv run pytest tests/test_discovery_workflow.py tests/test_discovery_common.py -q
git add app/discovery_routes.py app/main.py discovery/workflow.py tests/test_discovery_workflow.py
git commit -m "feat: expose guided discovery runs"
```

## Task 6: Add overrides, decisions, retry, and read-only replay

**Files:**
- Create: `discovery/overrides.py`
- Create: `tests/test_discovery_overrides.py`
- Modify: `discovery/workflow.py`
- Modify: `app/discovery_routes.py`
- Modify: `jobs/discovery.py`

- [ ] **Step 1: Write failing behavior tests**

Cover:

- overrides only in `waiting_confirmation`;
- forced pass/drop requires reason and never becomes a final decision;
- confirmed upstream stage cannot be edited;
- an explicit reopen operation invalidates downstream current attempts, preserves their rows for replay, creates a new upstream attempt, and allows corrected overrides before recomputation;
- while a stage is still waiting, posting the same code replaces its override; deleting it removes the override;
- retry only from transient `failed`, creates attempt+1, preserves attempt N;
- decisions use keep/exclude; exclude requires reason, keep note optional;
- all competition entrants need a decision before adjudication completes;
- 0–2 keepers end `insufficient_candidates`; 3–5 confirm; >5 rejects confirmation;
- historical GET renders persisted result/evidence without running current code.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_overrides.py tests/test_discovery_workflow.py -q
```

Expected: FAIL on missing endpoints/rules.

- [ ] **Step 3: Implement effective verdicts**

```python
def effective_verdict(result, override):
    return override.forced_verdict if override is not None else result.verdict
```

Overrides are mutable upserts/deletes only while their stage is `waiting_confirmation`; confirmation freezes them. A changed override after confirmation requires `reopen`.

- [ ] **Step 4: Add remaining endpoints**

Implement:

```text
POST .../stages/safety/retry
POST .../tracks/{track}/stages/{stage}/retry
POST .../tracks/{track}/stages/{stage}/confirm
POST .../tracks/{track}/stages/{stage}/reopen
POST .../overrides
DELETE .../overrides/{override_id}
POST .../decisions
POST .../abandon
POST .../recompute
```

`reopen` marks the selected and downstream attempts superseded/read-only, recreates and reruns the selected upstream attempt, pauses at `waiting_confirmation`, then permits override edits; confirming it recreates downstream stages. It never deletes prior results.

`recompute` never mutates an abandoned/historical run. If the saved build, ruler payload, and input snapshot are executable and no current-week conflict exists, it creates a new run with `replay_of_run_id`, copies the immutable inputs, and computes there; otherwise it returns `algorithm_unavailable` or the current-run conflict. Use the existing `_error_json` envelope and 409 for invalid state.

- [ ] **Step 5: Verify full framework**

```powershell
uv run pytest tests/test_discovery_workflow.py tests/test_discovery_common.py tests/test_discovery_overrides.py -q
uv run pytest -q
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```powershell
git add discovery/overrides.py discovery/workflow.py app/discovery_routes.py jobs/discovery.py tests/test_discovery_overrides.py tests/test_discovery_workflow.py
git commit -m "feat: complete discovery confirmation workflow"
```

## Plan 2 acceptance

- Weekly inputs are immutable after creation.
- Common safety is confirmed once before tracks start.
- Every computed stage pauses at `waiting_confirmation`.
- Confirm/retry/create operations are idempotent.
- Track failures are independent.
- Abandoned runs remain readable and become immutable.
- No production track ruler is required yet; Plan 3 supplies them.
