# Selection evidence source audit - 2026-10-22

## Scope and release decision

Controller inputs contain two issuers, two annual periods, eight retained provider responses and two official PDFs. The implementer recomputed all ten file hashes and reproduced all 20 exact Decimal provider-versus-recorded-official-value comparisons. PDF values and page references were inspected by the controller and supplied in source-audit-notes.md; the implementer has not re-extracted the PDF tables. **All seven new field mappings retain trusted=false. Production calculations using these automated mappings remain blocked by zero/missing/full-market gaps.**

## Official reports

- [Gree 2025 annual report](https://static.cninfo.com.cn/finalpage/2026-04-29/1225250396.PDF); SHA256 `7cc972f2d199e2ec3797cc5da479e8268afa59b77875770de3bcd3648242fb8c`.
- [Haier 2025 annual report](https://static.cninfo.com.cn/finalpage/2026-03-27/1225038336.PDF); SHA256 `14186f9f9b47f687c527ba14825664f3b09e51759d581636cddcc248f26e3660`.

Pages below are one-based physical PDF pages. Amounts are CNY yuan and consolidated. Period-end balances are net carrying amounts. Interest expense is cumulative. 2024 comparatives presented in the 2025 reports cannot establish what earlier selections knew.

## Field-by-field mapping

The seven evidence-only specifications live in immutable `SELECTION_FIELD_DICTIONARY`. Legacy `FIELD_DICTIONARY` remains unchanged so mutable `FinancialRow` persistence does not acquire unsupported attributes. Versioned selection adapters use the selection registry.

| Field | Provider report.column | Gree / Haier physical pages | Scope and readiness |
|---|---|---|---|
| accounts_receivable | RPT_DMSK_FN_BALANCE.ACCOUNTS_RECE | 86 / 118 | Period-end net amount; four nonzero comparisons pass |
| contract_assets | RPT_F10_FINANCE_GBALANCE.CONTRACT_ASSET | 86,133 / 118 | Period-end net amount; four nonzero comparisons pass |
| inventory | RPT_DMSK_FN_BALANCE.INVENTORY | 86 / 118 | Period-end net amount; four nonzero comparisons pass |
| total_equity_including_minority | RPT_DMSK_FN_BALANCE.TOTAL_EQUITY | 87 / 120 | Period-end including minority interests; four nonzero comparisons pass |
| interest_expense | RPT_F10_FINANCE_GINCOME.FE_INTEREST_EXPENSE | 88,175 / 122,202 | Cumulative independent interest-expense line; four nonzero comparisons pass |
| restricted_cash | No structured mapping asserted | 162-163 / unverified | Gree 2025/2024 monetary funds: 10577592111.08 / 36145202061.32; Haier missing; blocked |
| related_party_funds | No structured mapping asserted | Gree unverified / Haier p2 qualitative | Numeric scope and zero mapping unverified; blocked |

All five matching mappings remain NOT READY: genuine zero cases, field-level missing semantics and full-market validation are outstanding. Gree GINCOME.INTEREST_EXPENSE is 26783481.38 / 158700589.03 (financial-subsidiary cost), while FE_INTEREST_EXPENSE is 1964864912.15 / 2378372721.06. Haier INTEREST_EXPENSE is null while FE_INTEREST_EXPENSE is populated. Neither INTEREST_EXPENSE nor net FINANCE_EXPENSE is substituted. Gree cash 110553006650.89 / 113900461797.94 is not unrestricted cash; total restricted assets are not restricted monetary funds.

## Exact sample comparisons

| Issuer | Period | Field | Provider | Official | Decimal result |
|---|---|---|---|---|---|
| 000651.SZ | 2025 | accounts_receivable | 15987180598.76 | 15987180598.76 | pass |
| 000651.SZ | 2024 | accounts_receivable | 16831887388.06 | 16831887388.06 | pass |
| 000651.SZ | 2025 | contract_assets | 399828954.57 | 399828954.57 | pass |
| 000651.SZ | 2024 | contract_assets | 592399551.89 | 592399551.89 | pass |
| 000651.SZ | 2025 | inventory | 28183464259.53 | 28183464259.53 | pass |
| 000651.SZ | 2024 | inventory | 27910910515.55 | 27910910515.55 | pass |
| 000651.SZ | 2025 | total_equity_including_minority | 149791280674.7 | 149791280674.70 | pass |
| 000651.SZ | 2024 | total_equity_including_minority | 141513694947.97 | 141513694947.97 | pass |
| 000651.SZ | 2025 | interest_expense | 1964864912.15 | 1964864912.15 | pass |
| 000651.SZ | 2024 | interest_expense | 2378372721.06 | 2378372721.06 | pass |
| 600690.SH | 2025 | accounts_receivable | 27065839108.45 | 27065839108.45 | pass |
| 600690.SH | 2024 | accounts_receivable | 26494845510.56 | 26494845510.56 | pass |
| 600690.SH | 2025 | contract_assets | 1007695591.67 | 1007695591.67 | pass |
| 600690.SH | 2024 | contract_assets | 997963705.67 | 997963705.67 | pass |
| 600690.SH | 2025 | inventory | 46848790687.12 | 46848790687.12 | pass |
| 600690.SH | 2024 | inventory | 43189855697.96 | 43189855697.96 | pass |
| 600690.SH | 2025 | total_equity_including_minority | 125977001315.11 | 125977001315.11 | pass |
| 600690.SH | 2024 | total_equity_including_minority | 118801721185.64 | 118801721185.64 | pass |
| 600690.SH | 2025 | interest_expense | 2679484195.35 | 2679484195.35 | pass |
| 600690.SH | 2024 | interest_expense | 2727313494.33 | 2727313494.33 | pass |

## Retained raw responses

Response hashes identify actual retained bytes, distinct from official PDF hashes and canonical evidence IDs. Automated tests use synthetic data only.

- `000651.SZ-RPT_DMSK_FN_BALANCE-source.json`; observed UTC `2026-10-22T08:26:32.720767+00:00`; SHA256 `b2440317250146f4dd79df7fbf8eb20a7fe8c38489b276e4cb1d833bccfb43ed`; [request URL](https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DMSK_FN_BALANCE&columns=ALL&pageNumber=1&pageSize=12&sortColumns=REPORT_DATE&sortTypes=-1&filter=%28SECUCODE%3D%22000651.SZ%22%29).
- `000651.SZ-RPT_DMSK_FN_INCOME-source.json`; observed UTC `2026-10-22T08:26:32.935522+00:00`; SHA256 `ba6b0a59ca8247ef28467a4efc975d6fdb477204fd0be57b774121fd93cbd0c7`; [request URL](https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DMSK_FN_INCOME&columns=ALL&pageNumber=1&pageSize=12&sortColumns=REPORT_DATE&sortTypes=-1&filter=%28SECUCODE%3D%22000651.SZ%22%29).
- `000651.SZ-RPT_F10_FINANCE_GBALANCE-source.json`; observed UTC `2026-10-22T08:27:20.519239+00:00`; SHA256 `7697b987f4f852800e2b6522a61b8f185056043b0a6131a35ba036abc0b5b5ae`; [request URL](https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_FINANCE_GBALANCE&columns=ALL&pageNumber=1&pageSize=12&sortColumns=REPORT_DATE&sortTypes=-1&filter=%28SECUCODE%3D%22000651.SZ%22%29).
- `000651.SZ-RPT_F10_FINANCE_GINCOME-source.json`; observed UTC `2026-10-22T08:27:20.795982+00:00`; SHA256 `04e8bd7f24010dcd7933009031dd5a3cd010dee31787bf68ac6a7b76914577bf`; [request URL](https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_FINANCE_GINCOME&columns=ALL&pageNumber=1&pageSize=12&sortColumns=REPORT_DATE&sortTypes=-1&filter=%28SECUCODE%3D%22000651.SZ%22%29).
- `600690.SH-RPT_DMSK_FN_BALANCE-source.json`; observed UTC `2026-10-22T08:26:33.143489+00:00`; SHA256 `d74a0222c4bd0ef2b0c875d8cda89ee3e674b013ad69d8d9a638ce4c6f3bff1f`; [request URL](https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DMSK_FN_BALANCE&columns=ALL&pageNumber=1&pageSize=12&sortColumns=REPORT_DATE&sortTypes=-1&filter=%28SECUCODE%3D%22600690.SH%22%29).
- `600690.SH-RPT_DMSK_FN_INCOME-source.json`; observed UTC `2026-10-22T08:26:33.377626+00:00`; SHA256 `7b3c475ebe14659ade3eba416f2144072c656ded1f421e8f199172a7ad7bc956`; [request URL](https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DMSK_FN_INCOME&columns=ALL&pageNumber=1&pageSize=12&sortColumns=REPORT_DATE&sortTypes=-1&filter=%28SECUCODE%3D%22600690.SH%22%29).
- `600690.SH-RPT_F10_FINANCE_GBALANCE-source.json`; observed UTC `2026-10-22T08:27:21.079615+00:00`; SHA256 `48bd8309cf54ccd1ba464a496b9a5277c5d55c62af2aeffbdb2e752f0274d02e`; [request URL](https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_FINANCE_GBALANCE&columns=ALL&pageNumber=1&pageSize=12&sortColumns=REPORT_DATE&sortTypes=-1&filter=%28SECUCODE%3D%22600690.SH%22%29).
- `600690.SH-RPT_F10_FINANCE_GINCOME-source.json`; observed UTC `2026-10-22T08:27:21.337239+00:00`; SHA256 `46be05e3a8ad6b4fb5b96c52f6de29f8e1e2d1a1583a5eed6302f176bf9898d8`; [request URL](https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_FINANCE_GINCOME&columns=ALL&pageNumber=1&pageSize=12&sortColumns=REPORT_DATE&sortTypes=-1&filter=%28SECUCODE%3D%22600690.SH%22%29).

## Time and trust boundaries

Gree summary balance 2025 NOTICE_DATE is 2026-08-27; detailed NOTICE_DATE is 2026-04-29 and UPDATE_DATE is 2026-08-27. Detailed 2024 NOTICE_DATE remains 2025-04-28 while UPDATE_DATE is 2026-04-29. UPDATE_DATE is retained independently and never substituted for publication. Date-only publication becomes available at next midnight in Asia/Shanghai, converted to UTC. Production uses max(publication boundary, observed_at).

The raw-response adapter parses retained bytes with json.loads(parse_float=Decimal), hashes those bytes, and keeps every mapped provider value untrusted (or missing for absent/null lines). The response digest is not an official PDF digest. Existing float-parsing HTTP helpers must not feed this exact-financial path. A failed response raises; an empty/latest page does not assert complete coverage. No new network fetch or automatic production integration is enabled.

Standalone normalized parse/CLI rejects archive_verified=true even with a hash and arbitrary source text. Independent archive validation belongs to T16. Repository loading supports already validated archive_verified DTOs; only historical=True uses their publication boundary. Absent records remain absent; downstream historical evaluation must return historical_evidence_unavailable rather than fabricate returns.

A normalized status=verified is an explicit human assertion requiring verification={method:human_review,reviewer,reviewed_at}, an HTTPS source URL, SHA256 and report_location. It is not independent verification of the reviewer or URL. trusted=true never upgrades an untrusted/model record. Provider rows stay untrusted/missing regardless of caller flags. Re-review appends a new identity and supersedes_id.

## Import and metadata contract

Run `uv run python -m scripts.import_selection_evidence --db PATH --input PATH --dry-run` for a JSON array. Dry-run validates the full batch without opening/creating the database. Omitting --dry-run appends atomically and creates only the two evidence/coverage tables, indexes and immutable triggers; it does not run init_db legacy migrations. Explicit evidence_id must match canonical identity. Decimal text/Decimal/integer inputs normalize to non-exponent strings; binary floats, booleans and nonfinite values are rejected. Absent/null financial values never become zero.

The unchanged T01 DTO preserves provenance in reserved values keys: `_report_location` (physical page/table or provider report/row), `_verification` (human receipt JSON), `_archive_verification_source` (optional descriptive text, no trust). These are metadata strings, not financial values. Calculation code must ignore reserved keys. They participate in canonical identity. Caller-supplied reserved keys inside values are rejected; use explicit metadata inputs.

CoverageWindow complete=false explicitly retains failed/incomplete scope. Coverage is an explicit repository operation; the raw-response parser never infers absence of events. `load_coverage(..., cutoff=aware_datetime)` returns only windows created by that cutoff whose referenced evidence was available under production availability rules. It never applies a historical archive override. Omitting cutoff is an inventory read and must not certify a past decision.

## Outstanding source blockers

- Genuine numeric-zero cases and each new field's absent/null/blank semantics remain unverified. Synthetic zero/null tests verify software only.
- Restricted cash lacks Haier two-period values and a verified structured mapping. Related-party-funds numeric scope remains unverified for Gree; Haier p2 negative qualitative statement is not numeric zero across scopes. Unmapped fields remain null and require manually reviewed normalized official-report evidence.
- Full-market coverage cannot be inferred from two issuers. Missing/incomplete evidence must block corresponding production capabilities.
- Current restatements and comparative values do not prove original historical availability; archive validation remains T16 work.

## T06 current-market-cap source verification - 2026-10-22

This source is separate from the seven financial-extension mappings above. Two read-only public `RPT_VALUEANALYSIS_DET` requests returned actual `SECUCODE`, `TRADE_DATE`, `TOTAL_MARKET_CAP`, `CLOSE_PRICE` and `TOTAL_SHARES`. Both actual report dates were `2026-10-22 00:00:00`. Parsing retained bytes with `json.loads(parse_float=Decimal)` reproduced each total market cap as the exact product of the same returned day's closing price and total shares:

| Code | Market cap (CNY) | Price (CNY) | Shares | Observation (UTC) | Exact product |
|---|---|---|---|---|---|
| 000001.SZ | 219286875637.4 | 11.3 | 19405918198 | 2026-10-22T10:27:29.525960+00:00 | pass |
| 600519.SH | 1648220090102.49 | 1318.49 | 1250081601 | 2026-10-22T10:27:29.721918+00:00 | pass |

- [000001.SZ exact-date request](https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEANALYSIS_DET&columns=SECUCODE%2CTRADE_DATE%2CTOTAL_MARKET_CAP%2CCLOSE_PRICE%2CTOTAL_SHARES&pageNumber=1&pageSize=2&filter=%28SECUCODE%3D%22000001.SZ%22%29%28TRADE_DATE%3D%272026-10-22%27%29); response SHA256 `cddb625ec2b158de89e71069ab737a8547826cdf2827ac68fbdca82673a07caf`.
- [600519.SH exact-date request](https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEANALYSIS_DET&columns=SECUCODE%2CTRADE_DATE%2CTOTAL_MARKET_CAP%2CCLOSE_PRICE%2CTOTAL_SHARES&pageNumber=1&pageSize=2&filter=%28SECUCODE%3D%22600519.SH%22%29%28TRADE_DATE%3D%272026-10-22%27%29); response SHA256 `776865e12b32ee311a8fd0f402d60161b6c9e6821842a993a99b1c1e95879525`.

Actual raw responses are retained in ignored implementation scratch `.superpowers/sdd/2026-10-22-selection-quality-and-validation-plan/source-audit/current-market-cap/{code}-2026-10-22.json`; retained hashes were recomputed. Tests contain synthetic responses only. No production database was accessed.

**Source-semantic status: verified for this provider column/date boundary and these two observed samples. Whole-market production readiness: not established; T19 owns the coverage threshold and activation.** These comparisons verify current market-cap units and same-day source structure, not independent issuer-share accuracy, complete A-share coverage, point-in-time historical availability, or permanent endpoint availability. The bank sample validates source structure only; industrial cash metrics remain not applicable to banks. No financial-extension mapping trust changes.

The opt-in parser requires one matching code, a strictly recognized returned day equal to the requested market day, a finite positive total market cap, and no market day later than observation in Asia/Shanghai. Missing/mismatched dates produce no value; fetch time never becomes market day. It retains code, actual returned market date, UTC observation, source, URL and exact response hash. T07 must freeze all these fields; T09 activates consumption. No annual-share fallback, new runtime dependency or v7 integration was added.
