# Discovery Data Credibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide verified historical valuation and financial inputs for the three discovery tracks, with resumable full-market backfill and explicit readiness reporting.

**Architecture:** Extend the existing East Money client and `Financial` cache rather than add a dependency. Store monthly valuation points in a normalized table, map statement fields through a documented field dictionary, and reuse the cheap-board chunk/commit pattern for recoverable backfills.

**Tech Stack:** Python 3.12, SQLAlchemy 2, SQLite, httpx2, pytest, East Money datacenter APIs.

---

## File map

- Modify: `market/types.py` — add typed valuation points, security state, and discovery financial fields.
- Modify: `market/eastmoney.py` — fetch historical valuation and three-statement fields.
- Modify: `market/cheap.py` — retain exact listing/trading/status/industry metadata.
- Modify: `app/models.py` — add `ValuationHistory` and durable fill checkpoints; extend `Financial`.
- Modify: `app/db.py` — additive columns/indexes and financial-cache version migration.
- Create: `discovery/__init__.py` — package marker.
- Create: `discovery/field_dictionary.py` — source fields, formulas, units, missing rules.
- Create: `discovery/data_readiness.py` — deterministic coverage calculation.
- Create: `jobs/discovery_data.py` — resumable valuation/financial backfill.
- Modify: `pyproject.toml` — include the `discovery` package in wheel builds.
- Create: `tests/test_discovery_data.py` — parsing, persistence, progress, readiness.
- Modify: `tests/test_eastmoney.py` — source-specific mapping regressions.

## Task 1: Prove valuation and security-state endpoints

**Files:**
- Create: `tests/test_discovery_data.py`
- Modify: `market/types.py`
- Modify: `market/eastmoney.py`
- Modify: `market/cheap.py`

- [ ] **Step 1: Write the failing parser contract test**

Add a stub response for `RPT_VALUEANALYSIS_DET` with three dates, duplicate dates, a negative PE, and valid PB:

```python
def test_fetch_valuation_history_maps_monthly_pe_pb() -> None:
    class Stub(EastMoneyClient):
        def _get_json(self, url: str) -> dict:
            assert "RPT_VALUEANALYSIS_DET" in url
            assert 'SECURITY_CODE%3D%22600519%22' in url
            return {
                "success": True,
                "result": {
                    "data": [
                        {"TRADE_DATE": "2026-08-31", "PE_TTM": "19.2", "PB_MRQ": "7.1"},
                        {"TRADE_DATE": "2026-08-01", "PE_TTM": "20.0", "PB_MRQ": "7.3"},
                        {"TRADE_DATE": "2026-07-31", "PE_TTM": "-1", "PB_MRQ": "7.0"},
                    ]
                },
            }

    rows = Stub().fetch_valuation_history("600519.SH")
    assert [(row.month, row.trade_date, row.pe, row.pb) for row in rows] == [
        ("2026-08", "2026-08-31", Decimal("19.2"), Decimal("7.1")),
        ("2026-07", "2026-07-31", None, Decimal("7.0")),
    ]
```

Use actual returned field names from a live probe. If East Money returns `PETTM/PB` rather than `PE_TTM/PB_MRQ`, update the fixture and field dictionary; do not support guessed aliases without evidence.

- [ ] **Step 2: Run the test and verify failure**

Run:

```powershell
uv run pytest tests/test_discovery_data.py::test_fetch_valuation_history_maps_monthly_pe_pb -q
```

Expected: FAIL because `ValuationPoint` and `fetch_valuation_history` do not exist.

- [ ] **Step 3: Add the smallest typed contract**

In `market/types.py`:

```python
@dataclass(frozen=True)
class ValuationPoint:
    code: str
    month: str
    trade_date: str
    pe: Decimal | None
    pb: Decimal | None
    price: Decimal | None = None
    source: str = "eastmoney"
    ratio_method: str = "pe_ttm_pb_mrq"
```

Add `fetch_valuation_history(code: str) -> list[ValuationPoint]` to `MarketClient`, `EastMoneyClient`, and `FakeMarketClient`. Follow all result pages until the requested five-year start date; a single 500-row page is insufficient. Deduplicate only after all pages are read, retain the latest trading date in each month, and convert non-positive PE/PB to `None`. Persist an explicit ratio method/basis such as `pe_ttm_pb_mrq`; mixed methods are not comparable.

Add tests where one month's last trading day is on page 2 and where 61 valid months span page boundaries. Preserve raw positive ratios; do not winsorize storage. Loss years are represented by `pe=None`; invalid/non-positive PB is `pb=None`. Because PE/PB ratios are split-invariant when source market cap and accounting denominators are consistently adjusted, record the East Money ratio method/source instead of applying a second price adjustment.

- [ ] **Step 4: Run the focused test**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Probe and map security state**

Verify exact East Money fields/endpoints for:

- SH/SZ A-share scope and BJ exclusion;
- ST/*ST and delisting-arrangement status;
- exact listing date;
- suspension/current trading state;
- last valid trading date;
- industry and industry-classification version/as-of.

Add `SecurityState` (or equivalent fields on `CheapRow`) only after the probe. Reuse the existing East Money index-kline pattern to add `fetch_trading_dates(end, limit=6)` to real/fake clients. Test that a suspension is eligible only when its last valid price is within the latest five frozen exchange trading dates; do not use calendar-day subtraction.

Bump `market.cheap.METHOD` and test that cached universe rows from the old method are ignored rather than decoded without the new security/industry metadata.

- [ ] **Step 6: Add a live valuation probe, not a production fallback**

Run a temporary script against:

```text
https://datacenter-web.eastmoney.com/api/data/v1/get
?reportName=RPT_VALUEANALYSIS_DET
&columns=ALL
&pageSize=500
&pageNumber=1
&sortColumns=TRADE_DATE
&sortTypes=-1
&filter=(SECURITY_CODE="600519")
```

Verify at least 招商银行、贵州茅台、美的集团. Follow pagination and record exact fields, page counts, oldest/newest dates, and valid monthly point counts in test comments. If the endpoint cannot provide five years consistently, stop this plan and mark quality-discount valuation history blocked; do not replace it with current cross-sectional PE.

- [ ] **Step 7: Commit**

```powershell
git add market/types.py market/eastmoney.py market/cheap.py tests/test_discovery_data.py
git commit -m "feat: fetch monthly valuation history"
```

## Task 2: Map the minimum statement fields

**Files:**
- Create: `discovery/__init__.py`
- Create: `discovery/field_dictionary.py`
- Modify: `market/types.py`
- Modify: `market/eastmoney.py`
- Modify: `tests/test_discovery_data.py`
- Modify: `tests/test_eastmoney.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write three failing source-mapping tests**

Create separate fixtures for:

- `RPT_DMSK_FN_INCOME`: `TOTAL_OPERATE_INCOME`, parent net profit, deducted net profit.
- `RPT_DMSK_FN_BALANCE`: `TOTAL_ASSETS`, `TOTAL_EQUITY`, `MONETARYFUNDS`, `SHORT_LOAN`, `LONG_LOAN`, `BOND_PAYABLE`, `NONCURRENT_LIAB_1YEAR`.
- `RPT_DMSK_FN_CASHFLOW`: `NETCASH_OPERATE` and the verified cash-paid-for-long-assets field.
- verified financial-industry reports: total/core-tier-1 capital adequacy and asset-quality fields.
- verified trap/growth fields: impairment loss, government grants, asset-disposal gain, goodwill, and acquisition cash paid.

Assert:

```python
assert row.revenue == Decimal("1000000000")
assert row.net_assets == Decimal("600000000")
assert row.cash == Decimal("200000000")
assert row.interest_debt == Decimal("150000000")
assert row.capex == Decimal("80000000")
assert row.roic == Decimal("18.4")
assert row.deducted_net_income == Decimal("90000000")
assert row.core_tier1_capital_adequacy == Decimal("13.2")
assert row.income_tax == Decimal("20000000")
assert row.pretax_income == Decimal("120000000")
```

- [ ] **Step 2: Run tests and verify failure**

```powershell
uv run pytest tests/test_discovery_data.py -q
```

Expected: FAIL on missing fields/fetch methods.

- [ ] **Step 3: Add the field dictionary first**

Implement a `FieldSpec` that records source report/field, unit, consolidated basis, period type, derivation formula, financial-sector substitute, missing policy, and source as-of. Then define constants such as:

```python
FIELD_DICTIONARY = {
    "revenue": FieldSpec(report="RPT_DMSK_FN_INCOME", field="TOTAL_OPERATE_INCOME", unit="CNY", ...),
    "total_assets": FieldSpec(report="RPT_DMSK_FN_BALANCE", field="TOTAL_ASSETS", unit="CNY", ...),
    "net_assets": FieldSpec(report="RPT_DMSK_FN_BALANCE", field="TOTAL_EQUITY", unit="CNY", ...),
    "cash": FieldSpec(report="RPT_DMSK_FN_BALANCE", field="MONETARYFUNDS", unit="CNY", ...),
    "capex": FieldSpec(report="RPT_DMSK_FN_CASHFLOW", field="<verified field>", unit="CNY", ...),
}
```

The debt formula must remain visible:

```python
interest_debt = sum_known(
    short_loan, long_loan, bond_payable, noncurrent_liab_1year
)
```

Persist every debt component as well as the derived total. Add a comment that `NONCURRENT_LIAB_1YEAR` can overstate interest-bearing debt; preserve components in `metrics` when displaying deep-value evidence.

For ROIC, accept a direct source field only after the three-company cross-check. Otherwise map and persist pre-tax income and income-tax expense, use the median valid effective tax rate from the latest three annual periods capped to the ruler's valid range, persist invested-capital components, and derive:

```text
NOPAT = EBIT × (1 - normalized effective tax rate)
invested capital = average(net assets + interest debt - excess cash)
ROIC = NOPAT / invested capital
```

If the required inputs are absent or non-positive, ROIC is missing; do not substitute an unverified vendor value.

- [ ] **Step 4: Extend `FinancialRow` and fetch statements**

Add:

```python
revenue: Decimal | None = None
total_assets: Decimal | None = None
net_assets: Decimal | None = None
cash: Decimal | None = None
short_loan: Decimal | None = None
long_loan: Decimal | None = None
bond_payable: Decimal | None = None
noncurrent_liab_1year: Decimal | None = None
interest_debt: Decimal | None = None
roic: Decimal | None = None
pretax_income: Decimal | None = None
income_tax: Decimal | None = None
invested_capital: Decimal | None = None
deducted_net_income: Decimal | None = None
capital_adequacy: Decimal | None = None
core_tier1_capital_adequacy: Decimal | None = None
impairment_loss: Decimal | None = None
government_grants: Decimal | None = None
asset_disposal_gain: Decimal | None = None
goodwill: Decimal | None = None
acquisition_cash_paid: Decimal | None = None
```

Keep `fetch_financials` as the compatibility entry. Add a typed `DiscoveryCompanyData` DTO and `fetch_discovery_data(code)` protocol/client/fake method that returns security state, financial rows, valuation points, request count, and source as-of, and raises a typed per-company error. `_financials_for` may call focused private statement fetchers and merge by `(period, period_type)`.

- [ ] **Step 5: Verify against three real companies**

Cross-check one industrial, one bank, and one asset-light company against their disclosed annual reports. Permit at most 1% difference after unit normalization; otherwise mark the field mapping untrusted.

- [ ] **Step 6: Run focused tests**

```powershell
uv run pytest tests/test_eastmoney.py tests/test_discovery_data.py -q
```

Expected: PASS.

- [ ] **Step 7: Include the package and commit**

Create `discovery/__init__.py` and add `"discovery"` to `tool.hatch.build.targets.wheel.packages`.

```powershell
git add discovery market/types.py market/eastmoney.py tests/test_discovery_data.py tests/test_eastmoney.py pyproject.toml
git commit -m "feat: map discovery financial fields"
```

## Task 3: Persist valuation history and new financial columns

**Files:**
- Modify: `app/models.py`
- Modify: `app/db.py`
- Modify: `jobs/funnel.py`
- Modify: `jobs/cheap.py`
- Modify: `screen/replay.py`
- Modify: `tests/test_discovery_data.py`

- [ ] **Step 1: Write migration and round-trip tests**

Assert that `init_db()` creates:

```python
class ValuationHistory(Base):
    __tablename__ = "valuation_history"
    code: Mapped[str] = mapped_column(String, primary_key=True)
    month: Mapped[str] = mapped_column(String, primary_key=True)
    trade_date: Mapped[str] = mapped_column(String, nullable=False)
    pe: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    pb: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    source: Mapped[str] = mapped_column(String, nullable=False)
    ratio_method: Mapped[str] = mapped_column(String, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
```

Also assert all new `financials` columns survive a persist/read round trip.

Add durable operational tables:

```text
discovery_data_fills: id, status, total, completed, failed, request_count,
                      daily_budget, budget_day, daily_requests,
                      started_at, updated_at, last_error
discovery_data_checkpoints: fill_id, code, status, attempts, next_retry_at,
                            valuation_points, error, updated_at
```

Use a unique `(fill_id, code)` constraint.

- [ ] **Step 2: Run tests and verify failure**

```powershell
uv run pytest tests/test_discovery_data.py -q
```

Expected: FAIL because table/columns are absent.

- [ ] **Step 3: Add additive schema and synchronized persistence**

Extend `Financial` with the fields from Task 2. Update:

- `app/db.py::_ensure_financial_columns`
- `jobs/funnel.py::persist_financials`
- `jobs/cheap.py::_rows_from_db`
- `screen/replay.py` financial serialization/deserialization

Bump `FIN_FETCH_VERSION` only after the new fetcher is ready, because `_reset_stale_financials` deletes the old cache.

- [ ] **Step 4: Add valuation upsert**

In `jobs/discovery_data.py`:

```python
def persist_valuation_history(
    session: Session, rows: Iterable[ValuationPoint], *, fetched_at: datetime
) -> None:
    for row in rows:
        session.merge(ValuationHistory(...))
```

Persist `trade_date` and `ratio_method` with every monthly row, with a round-trip test that rejects mixing methods in one percentile series.

- [ ] **Step 5: Run compatibility tests**

```powershell
uv run pytest tests/test_discovery_data.py tests/test_cheap.py tests/test_screen.py tests/test_replay.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add app/models.py app/db.py jobs/discovery_data.py jobs/funnel.py jobs/cheap.py screen/replay.py tests/test_discovery_data.py
git commit -m "feat: persist discovery market data"
```

## Task 4: Add resumable full-market backfill

**Files:**
- Modify: `jobs/discovery_data.py`
- Modify: `market/types.py`
- Modify: `market/eastmoney.py`
- Modify: `app/main.py`
- Modify: `jobs/lock.py`
- Modify: `tests/test_discovery_data.py`

- [ ] **Step 1: Write failing progress/resume tests**

Use `FakeMarketClient` and assert:

- completed batches are committed before a later batch fails;
- restart resumes pending/retryable checkpoints and does not request completed codes;
- one-company fetch failures are explicit rather than indistinguishable empty lists;
- progress reports total/completed/failed/request_count/daily_budget/last_error/ETA;
- a duplicate start does not create a second fill;
- backfill never runs inside a GET request.
- the worker throttles with a fake clock, stops at the daily request budget, retries at most the configured cap, and releases the lock in `finally`.
- daily request usage survives restart and resets only when `budget_day` changes.

- [ ] **Step 2: Run tests and verify failure**

```powershell
uv run pytest tests/test_discovery_data.py -q
```

Expected: FAIL on missing fill job/API.

- [ ] **Step 3: Implement one resumable job**

Reuse `jobs/cheap.py::execute_cheap_fill`, but use the durable fill/checkpoint tables from Task 3. Add a per-company fetch method that raises a typed error; do not use the current batch `fetch_financials` path that suppresses individual failures.

```python
FILL_CHUNK = 20

def execute_discovery_data_fill(session, market, fill_id, policy, clock):
    try:
        for checkpoint in claim_pending_checkpoints(session, fill_id, FILL_CHUNK):
            throttle(policy, clock)
            result = market.fetch_discovery_data(checkpoint.code)
            persist_company_result(session, checkpoint, result)
            session.commit()
    finally:
        release(session, fill_id)
```

`RequestPolicy` contains measured requests/second, max attempts, daily budget, and maximum run time. Establish values from Task 1 probes and record them in the status payload. Update persisted `budget_day` and `daily_requests` before each external request so restart cannot reset the budget. ETA is based on observed completed-company throughput and may be null before the first batch.

- [ ] **Step 4: Expose explicit start/status APIs**

Add:

```text
POST /api/discovery-data/fill
GET  /api/discovery-data/status
```

Return 409 with the existing `JOB_LOCKED` envelope when another heavy job owns the lock. Add `JOB_TYPE_DISCOVERY_DATA` and stale-startup cleanup.

On startup, mark an interrupted fill `paused`, return `running` checkpoints to `pending`, preserve completed checkpoints, and allow explicit resume. A `GET` reports state only.

- [ ] **Step 5: Run tests**

```powershell
uv run pytest tests/test_discovery_data.py tests/test_health.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add jobs/discovery_data.py market/types.py market/eastmoney.py jobs/lock.py app/main.py tests/test_discovery_data.py
git commit -m "feat: backfill discovery data resumably"
```

## Task 5: Add deterministic readiness reporting

**Files:**
- Create: `discovery/data_readiness.py`
- Modify: `app/main.py`
- Modify: `tests/test_discovery_data.py`

- [ ] **Step 1: Write failing coverage tests**

Cover:

- listed≥5y with 60 valid months is valuation-ready;
- recent listing is company-level insufficient, not track-wide failure;
- track-wide valuation coverage below configured threshold is `data_not_ready`;
- missing capex blocks deep value but not quality discount;
- missing revenue/ROIC blocks GARP but not other tracks.
- missing capital adequacy blocks the financial deep-value branch but not non-financial companies or other tracks.

- [ ] **Step 2: Run tests and verify failure**

```powershell
uv run pytest tests/test_discovery_data.py -q
```

Expected: FAIL on missing readiness functions.

- [ ] **Step 3: Implement pure readiness functions**

```python
@dataclass(frozen=True)
class TrackReadiness:
    track: str
    ready: bool
    eligible: int
    complete: int
    coverage: Decimal
    missing_fields: tuple[str, ...]
```

Return data; do not mutate runs here. The initial coverage thresholds are not guessed constants: Task 1/4 measurement produces observed coverage and failure-rate evidence, then the user approves the activation threshold and reason in Plan 3.

- [ ] **Step 4: Add status payload**

Extend `GET /api/discovery-data/status` with per-track readiness and source as-of timestamps.

- [ ] **Step 5: Verify**

```powershell
uv run pytest tests/test_discovery_data.py -q
uv run pytest -q
uv build
uv run --isolated --with ./dist/berkshire_plus-0.1.0-py3-none-any.whl python -c "import discovery, discovery.data_readiness"
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```powershell
git add discovery/data_readiness.py app/main.py tests/test_discovery_data.py
git commit -m "feat: report discovery data readiness"
```

## Plan 1 acceptance

- Historical valuation source is verified on three representative companies.
- No guessed field enters production mapping.
- Financial and valuation backfills resume after partial failure.
- Readiness is track-specific and deterministic.
- Existing cheap/funnel/replay behavior still passes.
- Wheel installation can import `discovery` and its data modules.
- Plan 2 may now freeze these inputs into weekly discovery runs.
