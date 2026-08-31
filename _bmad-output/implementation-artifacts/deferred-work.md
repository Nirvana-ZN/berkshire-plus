# Deferred work

- source_spec: `_bmad-output/implementation-artifacts/spec-2-3-观察池与预填记账.md`
  summary: 工作台「最近任务」按 `updated_at` 混排后只显示最近 8 条，仍不是带日期的完整时间线。
  evidence: `GET /api/jobs` 混排漏斗/评估/审视；HomeView 用 `slice(0, 8)` 截断展示。

- source_spec: none
  summary: 一键审视可选三档深度：短体检、引用已有六关（无则短体检）、缺六关先评估再审视；不纳入四大师。
  evidence: 用户原意含「审视程度可选」且选定 1B；与看仓单票 Checklist 可独立交付，按 [S] 拆出后延。
