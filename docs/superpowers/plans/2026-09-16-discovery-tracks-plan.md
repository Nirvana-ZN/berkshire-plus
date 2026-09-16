# Three Discovery Tracks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement independently versioned quality-discount, deep-value, and GARP tracks that produce deterministic 15-company competition lists and explainable evidence cards.

**Architecture:** Each track is a pure classifier over frozen snapshot fields plus a versioned ruler payload. Shared metric helpers compute normalized series, while each track owns its gates, sector branches, ordering, and evidence rules; no cross-track score exists.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Decimal arithmetic, pytest.

---

## File map

- Modify: `app/models.py` — add `scope` to `RulerVersion`.
- Modify: `app/db.py` — migrate existing ruler rows to `scope="funnel"`.
- Modify: `screen/ruler.py` — preserve legacy funnel behavior while filtering by scope.
- Create: `discovery/rulers.py` — three complete versioned payloads and activation checks.
- Create: `discovery/tracks/__init__.py` — packaged track namespace.
- Create: `discovery/metrics.py` — CAGR, medians, stability, percentiles, normalized earnings.
- Create: `discovery/tracks/quality_discount.py`
- Create: `discovery/tracks/deep_value.py`
- Create: `discovery/tracks/garp.py`
- Create: `discovery/competition.py`
- Create: `discovery/evidence.py`
- Create: `discovery/summaries.py`
- Create: `discovery/diff.py`
- Modify: `jobs/discovery.py` — execute track gate, competition, adjudication evidence.
- Modify: `app/discovery_routes.py` — expose ruler readiness and stage results.
- Create: `scripts/calibrate_discovery.py` — print full-market distributions; never mutate rulers.
- Create: `tests/test_discovery_rulers.py`
- Create: `tests/test_discovery_tracks_quality.py`
- Create: `tests/test_discovery_tracks_deep_value.py`
- Create: `tests/test_discovery_tracks_garp.py`
- Create: `tests/test_discovery_competition.py`
- Create: `tests/test_discovery_evidence.py`
- Create: `tests/test_discovery_diff.py`

## Task 1: Add scoped ruler schema without activating track rules

**Files:**
- Modify: `app/models.py`
- Modify: `app/db.py`
- Modify: `screen/ruler.py`
- Create: `discovery/rulers.py`
- Create: `discovery/tracks/__init__.py`
- Create: `tests/test_discovery_rulers.py`
- Modify: `tests/test_ruler.py`

- [ ] **Step 1: Write failing compatibility tests**

Assert:

- existing ruler rows migrate to `scope="funnel"`;
- `screen.ruler.latest()` still returns only funnel scope;
- activating quality discount does not invalidate funnel;
- each discovery scope can have an independent latest version;
- an uncalibrated scope has no active version and is `ruler_not_ready`;
- a payload missing field/formula/threshold/min-observations/missing-policy/sector-branch/order/industry-version/build-version is rejected.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_ruler.py tests/test_discovery_rulers.py -q
```

Expected: FAIL because `RulerVersion.scope` and discovery rulers do not exist.

- [ ] **Step 3: Add additive migration and scoped queries**

Add:

```python
scope: Mapped[str] = mapped_column(String, nullable=False, default="funnel")
```

Update `latest`, `activate`, `require_matched`, and `status` to accept `scope="funnel"` while retaining current callers unchanged.

- [ ] **Step 4: Define complete ruler payload shape**

```python
@dataclass(frozen=True)
class TrackRuler:
    scope: str
    fields: tuple[str, ...]
    formulas: dict[str, str]
    thresholds: dict[str, str]
    min_observations: dict[str, int]
    missing_policy: dict[str, str]
    sector_branches: dict[str, object]
    order: tuple[tuple[str, str, str], ...]
    industry_version: str
    algorithm_version: str
```

Serialize Decimal thresholds as strings and hash canonical JSON.

Create `discovery/tracks/__init__.py`. This task defines payload schema/validation only; it must not seed or activate any discovery ruler.

- [ ] **Step 5: Run tests and commit**

```powershell
uv run pytest tests/test_ruler.py tests/test_discovery_rulers.py -q
git add app/models.py app/db.py screen/ruler.py discovery/rulers.py discovery/tracks/__init__.py tests/test_ruler.py tests/test_discovery_rulers.py
git commit -m "feat: add scoped discovery ruler ledger"
```

## Task 2: Add exact reusable financial metrics

**Files:**
- Create: `discovery/metrics.py`
- Modify: `tests/discovery_helpers.py`
- Create: `tests/test_discovery_metrics.py`

- [ ] **Step 1: Write table-driven failing tests**

Cover:

- CAGR with positive endpoints;
- CAGR returns `None` for missing/non-positive endpoints;
- median and mean use only finite annual values;
- coefficient of variation does not divide by zero;
- OCF/NI averages skip zero NI but require configured minimum years;
- normalized earnings use median deducted earnings, not one-year peak;
- valuation percentile ignores non-positive observations and returns exact 0–100 percentile;
- no float conversion occurs.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_metrics.py -q
```

Expected: FAIL because metrics module does not exist.

- [ ] **Step 3: Implement minimal Decimal helpers**

Functions:

```python
cagr(values: Sequence[Decimal], years: int) -> Decimal | None
finite_values(...)
mean(...)
median(...)
coefficient_of_variation(...)
cash_conversion(...)
normalized_earnings(...)
percentile_rank(current, history) -> Decimal | None
```

Use the standard library only.

- [ ] **Step 4: Run tests and commit**

```powershell
uv run pytest tests/test_discovery_metrics.py -q
git add discovery/metrics.py tests/discovery_helpers.py tests/test_discovery_metrics.py
git commit -m "feat: add discovery metric primitives"
```

## Task 2A: Define deterministic stage summaries

**Files:**
- Create: `discovery/summaries.py`
- Create: `tests/test_discovery_summaries.py`
- Modify: `app/discovery_routes.py`

- [ ] **Step 1: Write failing summary tests**

For each stage require: fixed rule text, ruler version/reason, input/pass/drop/insufficient/override counts, configured thresholds, metric distributions with count/min/p25/median/p75/max, and threshold marker positions. Empty/all-null metrics must return explicit unavailable summaries.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_summaries.py -q
```

- [ ] **Step 3: Implement Decimal-safe summaries**

Compute summaries from persisted stage results, never from mutable market caches. Return JSON-ready strings for exact Decimal values.

- [ ] **Step 4: Expose summaries on common and track-stage GET payloads**

Use one `summary` envelope for both route types so the Vue page can render them uniformly.

- [ ] **Step 5: Run and commit**

```powershell
uv run pytest tests/test_discovery_summaries.py -q
git add discovery/summaries.py app/discovery_routes.py tests/test_discovery_summaries.py
git commit -m "feat: summarize discovery stage distributions"
```

## Task 3: Deliver quality discount as the first vertical track

**Files:**
- Create: `discovery/tracks/quality_discount.py`
- Create: `tests/test_discovery_tracks_quality.py`
- Modify: `jobs/discovery.py`
- Modify: `app/discovery_routes.py`
- Modify: `discovery/rulers.py`
- Modify: `scripts/calibrate_discovery.py`

- [ ] **Step 1: Write failing gate tests**

Use a frozen snapshot fixture and assert:

- listed≥5y but <60 valid valuation months → insufficient;
- ROE10 < configured 12% → drop;
- five-year OCF/NI < 1 → drop;
- ROE level and stability are separate ruler gates/order fields;
- dilution and non-financial debt/interest coverage follow explicit ruler policies;
- deteriorating margins trigger deterministic risk evidence but only drop if ruler says so;
- PE/PB current percentile above the configured discount threshold → drop;
- current valuation must also meet the configured discount to its long-term median;
- cyclical companies use normalized deducted earnings instead of one-year peak PE;
- a bank uses its declared financial-sector branch;
- the same company can pass quality discount while failing another track fixture.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_tracks_quality.py -q
```

Expected: FAIL on missing track classifier and active ruler.

- [ ] **Step 3: Produce calibration evidence**

```powershell
uv run python scripts/calibrate_discovery.py --track quality_discount --output docs/discovery-calibration/quality-discount.json
```

The artifact records full-market field coverage, valid valuation months, missing reasons, metric quantiles, candidate counts across threshold bands, request/source as-of, and proposed overall readiness threshold. Present it to the user and obtain explicit approval plus change reason. Do not activate the ruler before this checkpoint.

- [ ] **Step 4: Activate the approved complete ruler**

Persist exact thresholds, minimum observations, missing policy, financial-sector branch, lexicographic order, industry classification version, data-readiness threshold, and algorithm build version.

- [ ] **Step 5: Implement a pure classifier**

```python
def classify(snapshot: FrozenCompany, ruler: TrackRuler) -> TrackVerdict:
    ...
```

Return `pass/drop/insufficient`, one primary reason, all computed metrics, and triggered evidence rule IDs. Never query the database.

- [ ] **Step 6: Wire track-gate execution**

`jobs.discovery.execute_track_gate(stage_id)` reads frozen snapshots and the exact ruler payload stored on the run, persists `DiscoveryResult`, and sets `waiting_confirmation`.

- [ ] **Step 7: Add API integration test**

Create run → confirm common safety → execute quality gate → GET results. Assert the other two tracks remain independent placeholders until their rules are activated.

- [ ] **Step 8: Verify and commit**

```powershell
uv run pytest tests/test_discovery_tracks_quality.py tests/test_discovery_workflow.py -q
git add discovery/tracks/quality_discount.py discovery/rulers.py jobs/discovery.py app/discovery_routes.py scripts/calibrate_discovery.py docs/discovery-calibration/quality-discount.json tests/test_discovery_tracks_quality.py
git commit -m "feat: add quality discount discovery track"
```

## Task 4: Add deterministic competition and evidence cards

**Files:**
- Create: `discovery/competition.py`
- Create: `discovery/evidence.py`
- Create: `tests/test_discovery_competition.py`
- Create: `tests/test_discovery_evidence.py`
- Modify: `jobs/discovery.py`

- [ ] **Step 1: Write failing competition tests**

For quality discount, configure lexicographic order:

```python
(
    ("valuation_percentile", "asc", "nulls_last"),
    ("roe_stability", "asc", "nulls_last"),
    ("cash_conversion", "desc", "nulls_last"),
    ("code", "asc", "nulls_last"),
)
```

Assert:

- exact tie breaks by code;
- unknown industry becomes insufficient before competition;
- max 2 per industry;
- max 15 total;
- companies excluded by industry cap retain reason/rank.

- [ ] **Step 2: Write failing evidence tests**

Evidence must include rule ID and input fields. Risks may come only from:

- metric near threshold;
- worsening trend;
- sector-specific risk;
- missing non-gating evidence.

If no deterministic risk fires, output `尚无结构化反方证据`; do not call LLM.

Every case card must persist 3–5 key evidence items. If fewer than three reliable track-specific facts are available, the candidate is insufficient rather than padded with generic text.

- [ ] **Step 3: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_competition.py tests/test_discovery_evidence.py -q
```

- [ ] **Step 4: Implement generic within-track ordering**

Read the sort tuple from the frozen ruler payload. Do not import a track module into `competition.py`.

- [ ] **Step 5: Generate evidence before adjudication**

Persist `DiscoveryEvidence` for every competition entrant, keyed by run/track/code/competition attempt/rule version.

- [ ] **Step 6: Run and commit**

```powershell
uv run pytest tests/test_discovery_competition.py tests/test_discovery_evidence.py -q
git add discovery/competition.py discovery/evidence.py jobs/discovery.py tests/test_discovery_competition.py tests/test_discovery_evidence.py
git commit -m "feat: rank and explain discovery candidates"
```

## Task 5: Add the deep-value track

**Files:**
- Create: `discovery/tracks/deep_value.py`
- Create: `tests/test_discovery_tracks_deep_value.py`
- Modify: `discovery/rulers.py`
- Modify: `jobs/discovery.py`

- [ ] **Step 1: Write separate financial/non-financial tests**

Non-financial branch:

- normalizes earnings from deducted net income;
- requires positive cumulative FCF from reliable OCF-capex;
- checks net debt/normalized FCF and interest cover;
- flags material dilution and revenue decline;
- rejects one-time-profit false cheapness.
- checks major impairment and the share of subsidies/asset-disposal gains in normalized profit;

Financial branch:

- does not apply industrial FCF/net-debt rules;
- uses PB, multi-year ROE, NPL trend, and verified capital-adequacy fields;
- missing capital adequacy is insufficient, not pass.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_tracks_deep_value.py -q
```

- [ ] **Step 3: Calibrate, then activate exact payload**

Run:

```powershell
uv run python scripts/calibrate_discovery.py --track deep_value --output docs/discovery-calibration/deep-value.json
```

Present coverage/distributions/candidate bands to the user. Only after explicit approval and a change reason, save thresholds, financial-sector substitutes, industry version, readiness threshold, and exact order in the active ruler payload; do not hard-code a hidden fallback.

- [ ] **Step 4: Implement branch classifiers**

Keep private `_classify_financial` and `_classify_nonfinancial` functions in the same module. Unknown branch returns insufficient.

- [ ] **Step 5: Run and commit**

```powershell
uv run pytest tests/test_discovery_tracks_deep_value.py tests/test_discovery_competition.py -q
git add discovery/tracks/deep_value.py discovery/rulers.py jobs/discovery.py scripts/calibrate_discovery.py docs/discovery-calibration/deep-value.json tests/test_discovery_tracks_deep_value.py
git commit -m "feat: add deep value discovery track"
```

## Task 6: Add the GARP track

**Files:**
- Create: `discovery/tracks/garp.py`
- Create: `tests/test_discovery_tracks_garp.py`
- Modify: `discovery/rulers.py`
- Modify: `jobs/discovery.py`
- Modify: `scripts/calibrate_discovery.py`

- [ ] **Step 1: Write failing growth-quality tests**

Cover:

- fewer than configured annual observations → insufficient;
- revenue and deducted-profit CAGR are computed over exact periods;
- one-year low-base spike does not satisfy sustained growth;
- latest-year growth slowdown is compared with the multi-year trend;
- ROIC is primary; configured ROE fallback is labeled in metrics;
- OCF/NI and dilution catch low-quality growth;
- interest-debt/goodwill/acquisition-cash trends flag leverage- or M&A-driven growth;
- gross/net margin deterioration acts as the first-version unit-economics check;
- growth used in PEG is capped by ruler configuration;
- historically extreme PE fails even when PEG looks low.

Any required impairment/subsidy/M&A/margin input that the active ruler marks as gating but the source cannot provide yields `insufficient`; it is never silently skipped.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_tracks_garp.py -q
```

- [ ] **Step 3: Calibrate and activate**

```powershell
uv run python scripts/calibrate_discovery.py --track garp --output docs/discovery-calibration/garp.json
```

Present the artifact to the user. After explicit approval and a change reason, persist min observations, CAGR/ROIC/cash-conversion gates, growth cap, valuation ceiling, industry version, readiness threshold, and lexicographic order.

- [ ] **Step 4: Implement the pure classifier**

No database calls and no LLM. Preserve each failed gate as structured metrics; choose the first ruler-ordered failure as the primary reason.

- [ ] **Step 5: Run and commit**

```powershell
uv run pytest tests/test_discovery_tracks_garp.py tests/test_discovery_competition.py -q
git add discovery/tracks/garp.py discovery/rulers.py jobs/discovery.py scripts/calibrate_discovery.py docs/discovery-calibration/garp.json tests/test_discovery_tracks_garp.py
git commit -m "feat: add GARP discovery track"
```

## Task 7: Classify week-over-week changes

**Files:**
- Create: `discovery/diff.py`
- Create: `tests/test_discovery_diff.py`
- Modify: `app/discovery_routes.py`

- [ ] **Step 1: Write failing cause tests**

Assert:

- same ruler, changed input → `data_changed`;
- changed ruler, same comparable input → `rule_changed`;
- both changed → `both_changed`, never claim one cause;
- only adjudication changed → `decision_changed`;
- first appearance → `new`;
- prior candidate absent now → `dropped`.
- selected in consecutive weeks → `continuous`;
- absent for one or more weeks then selected again → `reentered`;
- unchanged membership but changed primary reason → `reason_changed` with the same data/rule/both cause classification.

- [ ] **Step 2: Run and verify failure**

```powershell
uv run pytest tests/test_discovery_diff.py -q
```

- [ ] **Step 3: Implement persisted-result comparison**

Compare per-company frozen input fingerprints, ruler IDs, results, and decisions across ordered weekly history. The run-level fingerprint is only a batch integrity check. Do not rerun old algorithm code for normal history display.

- [ ] **Step 4: Expose changes in current-run GET**

Return compact `changes[]`; keep per-row details on the typed stage endpoint.

- [ ] **Step 5: Verify all tracks**

```powershell
uv run pytest tests/test_discovery_rulers.py tests/test_discovery_metrics.py tests/test_discovery_summaries.py tests/test_discovery_tracks_quality.py tests/test_discovery_tracks_deep_value.py tests/test_discovery_tracks_garp.py tests/test_discovery_competition.py tests/test_discovery_evidence.py tests/test_discovery_diff.py -q
uv run pytest -q
uv build
uv run --isolated --with ./dist/berkshire_plus-0.1.0-py3-none-any.whl python -c "import discovery.tracks.quality_discount, discovery.tracks.deep_value, discovery.tracks.garp"
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```powershell
git add discovery/diff.py app/discovery_routes.py tests/test_discovery_diff.py
git commit -m "feat: explain weekly discovery changes"
```

## Plan 3 acceptance

- Three tracks produce different verdicts from the same frozen company.
- Every active ruler is complete, versioned, and deterministic.
- No cross-track score exists.
- Competition returns at most 15 with industry cap and stable tie-breaking.
- Evidence cards are deterministic and traceable.
- Weekly changes distinguish data, ruler, both, and decision changes.
- Installed wheel imports every track module.
