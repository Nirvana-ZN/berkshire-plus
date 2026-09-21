# East Money discovery financial-field calibration

Live-verified at `2026-09-21T11:08:47Z` through
`EastMoneyClient._financial_report_rows(..., before_request=gate)`. The gate
and client each counted all six requests (two reports for each company):

```text
GET https://datacenter-web.eastmoney.com/api/data/v1/get
  ?reportName={REPORT}
  &columns=ALL&pageNumber=1&pageSize=50
  &sortColumns=REPORT_DATE&sortTypes=-1
  &filter=(SECUCODE="{CODE}")
```

The latest strict annual row (`REPORT_DATE=2025-12-31`) produced:

- `600519.SH` (Kweichow Moutai, industrial): `ROEJQ=32.53`,
  `TOTAL_SHARE=1250081601`, `XSMLL=91.1795516835`,
  `XSJLL=50.5278865155`, `INTSTCOVRATE=3994.57814905189`,
  `OTHER_INCOME=28531529.3`; source notice/update `2026-04-17`.
- `601899.SH` (Zijin Mining, cyclical/nonfinancial): `ROEJQ=33.04`,
  `TOTAL_SHARE=26590714622`, `XSMLL=27.7273101047`,
  `XSJLL=18.2830174371`, `INTSTCOVRATE=21.533919468562`,
  `OTHER_INCOME=563179942`; source notice/update `2026-03-21`.
- `600036.SH` (China Merchants Bank): `ROEJQ=13.44`,
  `TOTAL_SHARE=25219845601`, `XSJLL=44.7738288518`; `XSMLL`,
  `INTSTCOVRATE`, and `OTHER_INCOME` were null; source notice/update
  `2026-03-28`.

`ROEJQ`, `TOTAL_SHARE`, `XSMLL`, `XSJLL`, and `INTSTCOVRATE` come from
`RPT_F10_FINANCE_MAINFINADATA`. `OTHER_INCOME` comes from
`RPT_F10_FINANCE_GINCOME`. The sampled payloads did not expose
`ROE`, `WEIGHTAVGROE`, `TOTALSHARE`, `TOTAL_SHARES`,
`GROSSPROFITMARGIN`, or `NETPROFITMARGIN`, so strict discovery does not
silently accept those aliases.

`OTHER_INCOME` is retained under its source name. It is not asserted to be
government grants; rulers may declare it only as a conservative non-core-income
proxy. Bank rows do not require non-applicable gross-margin or
interest-coverage fields.
