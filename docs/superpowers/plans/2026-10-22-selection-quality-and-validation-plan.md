# 选股质量、证据与效果验证 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成三轨选股的口径纠偏、冻结证据、经营确认、独立效果验证以及研究和组合层集成。

**Architecture:** 保留现有抓取、冻结、三轨、研究和持仓流程，新增证据账本与纯计算模块；效果验证使用独立的纸面账本。所有影响判断的输入版本化，候选构建统一升级到 discovery-v8，旧批次只读。19个任务按依赖执行，每个任务有自己的失败测试、实现、回归和提交。

**Tech Stack:** Python 3.12–3.14、FastAPI、SQLAlchemy、SQLite、Vue 3、TypeScript、pytest；依赖版本沿用仓库。

**Spec:** [2026-10-22-selection-quality-and-validation-design.md](../specs/2026-10-22-selection-quality-and-validation-design.md)

## Global Constraints

- Python `>=3.12,<3.15`；Node `^22.18.0 || >=24.12.0`；沿用 FastAPI、SQLAlchemy、SQLite、Vue 3、TypeScript、pytest，不新增运行时依赖。
- 金额使用人民币元、股数使用股、内部增长率使用小数、现有 ROE/毛利率输入使用百分数；单位转换仅在明确的边界函数发生。
- 金融计算使用有限 `Decimal`；JSON 中新增金融数值写十进制字符串，禁止 NaN、Infinity 和二进制浮点排序。
- 业务日历使用 `Asia/Shanghai`，持久化时间使用带时区 UTC；日期值不得冒充精确公告时间。
- 不修改已经冻结的批次、尺子、证据、原始报表和已发布跟踪配置；修订一律追加版本并关联被修订记录。
- 缺失、不适用、来源不可信、确认为零是四种不同状态；任何一种都不得静默变成“无风险”。
- 测试只使用临时库和去标识 fixture；禁止对 `data/app.db` 运行测试、迁移演练、删库或重建。
- 三轨只输出研究资格；纸面组合、研究结论和仓位提示不产生真实交易指令。
- 每个改变准入、排序、风险等级、价格带或组合约束的参数都必须进入版本化配置和输入指纹。

## 0. 执行基线与依赖

本文件是开发文档，不是已执行报告。所有复选框初始未完成；代码块是要写入指定文件的合同、核心算法或测试样例，不能把文档中的 Expected 当作测试结果。用户当前授权为完成设计与开发文档，后续执行本计划时再读取当时工作区，不覆盖既存改动。

核查基线为 `3c6a063`、schema3、discovery-v7、run8、尺子21/22/23。开工先运行 `git status --short`、`git log -1 --oneline` 和下述版本命令，记录差异；新的业务需求不得通过修改这份基线悄悄扩大范围。

```powershell
.venv/Scripts/python.exe --version
node --version
uv run pytest tests/test_discovery_competition.py tests/test_discovery_metrics.py -q
```

Windows 优先 `uv run` 使用项目解释器，不能调用 PATH 上的 Python3.8。命令从仓库根运行。每任务提交只暂存该任务的确切文件，禁止 `git add .`。任务末尾的 commit 是开发时的动作，本次只提交两份文档。

依赖：T01→T02→T03；T04依赖T01；T05/T06依赖T01—T03；T07/T08依赖T01—T03；T09依赖T04—T08；T10依赖T03/T06；T11依赖T02/T09/T10；T12依赖T07/T11；T13依赖T11；T14依赖T02/T09；T15依赖T03/T14；T16依赖T15；T17依赖T09/T11/T12/T16；T18依赖T10/T12；T19依赖全部任务。M1=T01—T07/T09，M2=T08/T10，M3=T13—T16，M4=T11/T12/T17—T19。

文件职责在设计第4节。新增表统一放 `app/selection_models.py`，注册到 `init_db()` 的 metadata；纯计算文件不导入 Session、HTTP 客户端或 LLM。`tracking` 包在T14加入wheel packages。接口名称以下文 Interfaces 为准；新类型必须在使用它的任务前定义。

## T01：建立事实与计算 DTO 合同

**Files:** Create `discovery/fundamental_types.py`, `tests/selection_helpers.py`, `tests/test_fundamental_types.py`。

**Interfaces:** 导出 `EvidenceRecord`, `CoverageWindow`, `CapitalEvent`, `MetricResult`, `OperatingResult`, `FundamentalSnapshot`，以及 `evidence_identity(record:EvidenceRecord)->str`；后续所有计算使用这些类型。所有字段无隐式单位转换。

- [ ] 写失败测试，证明非法状态、无时区时间、NaN和可变输入不能进入冻结事实。

```python
from dataclasses import FrozenInstanceError
from decimal import Decimal
import pytest
from discovery.fundamental_types import MetricResult

def test_metric_keeps_unknown_distinct_from_zero():
    zero = MetricResult(Decimal('0'), 'verified', None, ())
    unknown = MetricResult(None, 'missing', 'not_reported', ())
    assert zero != unknown
    with pytest.raises(ValueError):
        MetricResult(Decimal('NaN'), 'verified', None, ())
    with pytest.raises(FrozenInstanceError):
        zero.status = 'missing'
```

- [ ] Run `uv run pytest tests/test_fundamental_types.py -q`；Expected：新模块/合同缺失导致失败。
- [ ] 写不可变dataclass与校验。字符串枚举必须按设计集合验证；映射构造时复制为MappingProxyType。核心返回类型：

```python
from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True)
class MetricResult:
    value: Decimal | None
    status: str
    reason: str | None
    evidence_ids: tuple[str, ...]

    def __post_init__(self):
        if self.status not in {'verified', 'missing', 'untrusted', 'not_applicable'}:
            raise ValueError('invalid metric status')
        if self.value is not None and not self.value.is_finite():
            raise ValueError('nonfinite metric')
        if (self.status == 'verified') != (self.value is not None):
            raise ValueError('value/status mismatch')
```

其余类型精确字段：`EvidenceRecord(evidence_id:str, code:str, kind:str, period:str, published_at:datetime, observed_at:datetime, source_updated_at:datetime|None, source_url:str, document_sha256:str, values:Mapping[str,str|None], status:str, basis_id:str, supersedes_id:str|None=None, date_only:bool=False, archive_verified:bool=False)`；`CoverageWindow(code:str,kind:str,start:date,end:date,complete:bool,evidence_ids:tuple[str,...])`；`CapitalEvent(event_id:str,code:str,kind:str,effective_at:datetime,sequence:int,shares_before:Decimal,shares_after:Decimal,factor:Decimal,evidence_id:str)`；`OperatingResult(state:str,period:str|None,reasons:tuple[str,...],metrics:Mapping[str,MetricResult])`；`FundamentalSnapshot(code:str,cutoff_at:datetime,annuals:tuple[EvidenceRecord,...],interims:tuple[EvidenceRecord,...],actions:tuple[CapitalEvent,...],extensions:tuple[EvidenceRecord,...],coverage:tuple[CoverageWindow,...],current_mcap:Decimal|None,fingerprint:str)`。evidence_identity对除evidence_id外所有字段生成规范JSON摘要：UTC时间ISO格式，映射按键排序，UTF-8、无空白、ensure_ascii=False、禁止非有限数；入库时核对身份，dataclass本身只检查字段合法性。

在 `tests/selection_helpers.py` 定义可直接复用的证据工厂；任何数值fixture用虚构代码和可识别测试URL，不拿用户库数据入测试：

```python
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from discovery.fundamental_types import EvidenceRecord, evidence_identity

def evidence(period: str, **values: str) -> EvidenceRecord:
    digest = sha256(repr((period, sorted(values.items()))).encode()).hexdigest()
    at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    row = EvidenceRecord(digest, '000001.SZ', 'financial', period, at, at,
                         None, 'https://example.invalid/fixture', digest,
                         values, 'verified', 'fixture-consolidated-cny')
    return replace(row, evidence_id=evidence_identity(row))
```

- [ ] 重跑上述测试，增加证据字典原对象修改不影响DTO、同ID冲突、date_only转换案例；Expected：全部通过。
- [ ] Commit `feat: define immutable selection evidence contracts`。

## T02：追加式证据存储与来源适配

**Files:** Create `app/selection_models.py`, `discovery/evidence_store.py`, `market/evidence.py`, `scripts/import_selection_evidence.py`, `tests/test_selection_evidence_store.py`, `tests/test_selection_evidence_sources.py`, `tests/fixtures/selection/evidence.json`, `docs/discovery-calibration/selection-evidence-source-audit.md`; Modify `app/db.py`, `discovery/field_dictionary.py`, `market/eastmoney.py`。

**Interfaces:** `append_evidence(session:Session,record:EvidenceRecord)->str`；`load_evidence_as_of(session:Session,code:str,cutoff:datetime,*,historical:bool=False)->tuple[EvidenceRecord,...]`；`available_at(record:EvidenceRecord,*,historical:bool=False)->datetime`；`parse_source_evidence(payload:Mapping[str,object])->EvidenceRecord`。CLI：`uv run python -m scripts.import_selection_evidence --db PATH --input PATH --dry-run`；不传dry-run才追加，不能更新旧证据。

- [ ] 写失败测试：同ID同内容幂等、同ID异内容拒绝、修改/删除触发数据库保护、缺失字段不补0、来源更新时间不等于公告时间。

```python
from dataclasses import replace
from datetime import datetime, timezone
from discovery.evidence_store import available_at
from tests.selection_helpers import evidence

def test_first_observation_is_not_backdated():
    row = replace(evidence('2025'),
        published_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert available_at(row, historical=False) == row.observed_at
```

- [ ] Run `uv run pytest tests/test_selection_evidence_store.py tests/test_selection_evidence_sources.py -q`；Expected：新接口缺失失败。
- [ ] 建表和适配器。证据表主键evidence_id、索引(code,kind,period,available_at)；覆盖表存区间、complete及证据ID；其他8张表在后续任务按同一metadata新增，列与唯一性按设计5.4。迁移仅建表/索引，使用显式列名序列化。

```python
from datetime import timedelta, timezone
from discovery.constants import CHINA_MARKET_TIMEZONE

def available_at(record, *, historical=False):
    published = record.published_at
    if record.date_only:
        local = published.astimezone(CHINA_MARKET_TIMEZONE)
        published = (local.replace(hour=0, minute=0, second=0, microsecond=0)
                     + timedelta(days=1)).astimezone(timezone.utc)
    if historical and record.archive_verified:
        return published
    return max(published, record.observed_at)
```

date_only必须先在中国时区做日界转换再转UTC；测试23:59、UTC跨日。保存新字段的source_url/document_sha256/报表位置，归属期/合并口径/单位在字典中固定。样本核验每个新字段至少两家、两期，覆盖“确实0”和缺失；不能只因接口列名相似就trusted=true。没有核实映射的字段走规范化JSON导入并标缺失，不猜东方财富字段代码。

- [ ] 重跑，加后来重述不能替换旧cutoff可见版本、真实归档与普通现拉数据差异测试；Expected：全部通过。来源核验输出 `docs/discovery-calibration/selection-evidence-source-audit.md`，逐列记录报表链接、样本、单位与通过/未就绪，未就绪项阻止对应生产能力。
- [ ] Commit `feat: persist versioned fundamentals with source provenance`。

## T03：公司行动与完整覆盖区间

**Files:** Create `market/corporate_actions.py`, `tests/test_corporate_actions.py`, `tests/fixtures/selection/capital-events.json`; Modify `market/cninfo.py`, `market/evidence.py`, `discovery/evidence_store.py`。

**Interfaces:** `parse_capital_events(records:Sequence[EvidenceRecord])->tuple[CapitalEvent,...]`；`fetch_event_evidence(code:str,start:date,end:date,*,before_request:Callable[[],None])->tuple[tuple[EvidenceRecord,...],CoverageWindow]`。返回合同可由公开源适配器或规范JSON导入满足，不能将最新8条公告函数充当全历史查询。

- [ ] 写失败测试：提案不生成已实施行动、分页中断coverage.complete=false、同一注销在不同进度公告中只记一次、同日事件按已核实sequence排序。

```python
from dataclasses import replace
from market.corporate_actions import parse_capital_events
from tests.selection_helpers import evidence

def test_proposed_bonus_is_not_an_effective_event():
    row = replace(evidence('2026', stage='proposed', action='bonus'), kind='capital_event')
    assert parse_capital_events((row,)) == ()
```

- [ ] Run `uv run pytest tests/test_corporate_actions.py tests/test_cninfo.py -q`；Expected：新增测试失败，旧公告测试仍可运行。
- [ ] 实现区间分页、公告ID去重、生效日期和股本前后衔接解析；完整响应的total/page范围全部取得且来源覆盖期间充分才写complete=true。

```python
def implemented_records(records):
    return tuple(r for r in records
                 if r.status == 'verified'
                 and r.values.get('stage') == 'implemented'
                 and r.values.get('effective_at'))
```

计划、实施、回购成交、注销采用不同kind与stage，原始记录保留；对所有网络请求接入现有RequestPolicy限流/中断/预算。对于无结构化可验证明细的公告，仅保存来源并返回不完整，规范JSON导入补齐后可重新生成新的完整覆盖版本。

- [ ] 重跑并加入拆合股、配股、股权激励、回购未注销、公告修订/撤销与两次重试；Expected：全部通过。
- [ ] Commit `feat: capture corporate actions with explicit coverage`。

## T04：修正并列、缺失和分支排名

**Files:** Create `discovery/ranking.py`, `tests/test_selection_ranking.py`; Modify `discovery/competition.py`, `tests/test_discovery_competition.py`。

**Interfaces:** `normalized_ranks(values:Mapping[str,Decimal|None],direction:str)->dict[str,Decimal]`；`rank_candidates(candidates:Sequence[CompetitionCandidate],contract:Mapping[str,object])->tuple[CompetitionVerdict,...]`。contract含branch_orders/risk_policy/industry_cap/total_cap；贡献明细由 `ranking_audit(candidates,contract)->dict[str,dict[str,object]]` 返回，名次和审计共用同一计算结果。

- [ ] 写失败测试：相同值同分、全空列不改排序、改代码不改非并列顺序、银行与非金融缺失含义不同。

```python
from decimal import Decimal as D
from discovery.ranking import normalized_ranks

def test_equal_values_share_midrank():
    assert normalized_ranks({'a':D('1'),'b':D('1'),'c':D('3')}, 'asc') == {
        'a':D('0.25'),'b':D('0.25'),'c':D('1')}

def test_empty_dimension_has_no_contribution():
    assert normalized_ranks({'a':None,'b':None}, 'asc') == {}
```

- [ ] Run `uv run pytest tests/test_selection_ranking.py tests/test_discovery_competition.py -q`；Expected：新增接口测试失败。
- [ ] 核心算法用Decimal平均名次；先按值组形成并列，不能把代码混入组内分值。

```python
from decimal import Decimal as D

def normalized_ranks(values, direction):
    if direction not in {'asc', 'desc'}:
        raise ValueError('invalid rank direction')
    if not values or len(set(values.values())) == 1:
        return {}
    groups = {}
    for code, value in values.items():
        if value is not None and not value.is_finite():
            raise ValueError('nonfinite rank value')
        groups.setdefault(value, []).append(code)
    keys = sorted((v for v in groups if v is not None), reverse=direction == 'desc')
    if None in groups:
        keys.append(None)
    out, start, n = {}, 1, len(values)
    for value in keys:
        codes = groups[value]
        rank = D(2 * start + len(codes) - 1) / 2
        score = (rank - 1) / (n - 1) if n > 1 else D('0.5')
        out.update({code:score for code in codes})
        start += len(codes)
    return out
```

全无信息时聚合层返回0.5；跨分支风险层一致，分支分位只代表相对排队。风险按operating state和核实风险类型去重，不数文案条数。名额逻辑复用原排序之后的行业/总数限制；不要改变同业最多2家。

- [ ] 重跑，加入上一轮评估的3股票复现、部分空值、输入顺序置换、分支只有1家、同一风险重复三次、各维贡献求和一致；Expected：全部通过。
- [ ] Commit `fix: make selection ranking invariant to ties and empty dimensions`。

## T05：经济稀释替换原始股本膨胀

**Files:** Create `discovery/capital_actions.py`, `tests/test_selection_dilution.py`; Modify `discovery/tracks/quality_discount.py`, `discovery/tracks/deep_value.py`, `discovery/tracks/garp.py` 的稀释调用（接入在T09统一启用）。

**Interfaces:** `economic_dilution(start_shares:Decimal,end_shares:Decimal,events:Sequence[CapitalEvent],*,coverage_complete:bool)->MetricResult`；`capital_change_audit(start_shares:Decimal,end_shares:Decimal,events:Sequence[CapitalEvent],*,coverage_complete:bool)->Mapping[str,MetricResult]` 输出净经济稀释/毛发行/注销/衔接误差。

- [ ] 写失败测试。事件单位均为实际股份；测试中的201→220不能被当成完整10转10。

```python
from datetime import datetime, timezone
from decimal import Decimal as D
from discovery.fundamental_types import CapitalEvent
from discovery.capital_actions import economic_dilution

def test_bonus_does_not_dilute_existing_owners():
    event = CapitalEvent('e','000001.SZ','bonus',datetime(2026,1,1,tzinfo=timezone.utc),
                        1,D('100'),D('200'),D('2'),'source')
    result = economic_dilution(D('100'),D('200'),(event,),coverage_complete=True)
    assert result.value == D('0')
```

- [ ] Run `uv run pytest tests/test_selection_dilution.py -q`；Expected：缺模块失败。
- [ ] 实现按effective_at/sequence逐项衔接，容差max(1股,前后股本较大值×0.0001)，先验证覆盖与链条再计算。公式核心：

```python
from decimal import Decimal
from math import prod

def adjusted_net_dilution(start_shares, end_shares, events):
    factors = (e.factor for e in events if e.kind in {'split','bonus','consolidation'})
    factor = prod(factors, start=Decimal('1'))
    return end_shares / (start_shares * factor) - 1
```

对事件链失败返回MetricResult(None,'untrusted','capital_actions_unreconciled',ids)，覆盖不足返回missing。毛发行/注销按当时股本转为窗口起始基础再累计，不对不同股本基础简单相加。

- [ ] 重跑，补纯转增0、转增+增发10%、注销后净缩股、发行/注销相抵但毛额不为0、未知事件、重复事件、股本缺失；Expected：全部通过。
- [ ] Commit `fix: distinguish economic dilution from bonus share changes`。

## T06：净现金、FCF和当前市值口径

**Files:** Create `discovery/cash_quality.py`, `tests/test_selection_cash_quality.py`; Modify `market/types.py`, `market/eastmoney.py`, `discovery/field_dictionary.py`, `discovery/tracks/deep_value.py`。

**Interfaces:** `cash_metrics(*,cash:Decimal|None,restricted_cash:Decimal|None,debt:Decimal|None,fcf:Decimal|None,mcap:Decimal|None)->dict[str,MetricResult]`；输出usable_cash/debt_burden/usable_net_cash_to_mcap/fcf_yield。同日市值进入新增冻结字段current_mcap和来源时间。

- [ ] 写失败测试，首先锁住不会奖励更低FCF：

```python
from decimal import Decimal as D
from discovery.cash_quality import cash_metrics

def test_less_fcf_never_improves_net_cash_debt_rank():
    a = cash_metrics(cash=D('100'),restricted_cash=D('0'),debt=D('0'),fcf=D('10'),mcap=D('200'))
    b = cash_metrics(cash=D('100'),restricted_cash=D('0'),debt=D('0'),fcf=D('1'),mcap=D('200'))
    assert a['debt_burden'].value == b['debt_burden'].value == 0
    assert a['fcf_yield'].value > b['fcf_yield'].value
```

- [ ] Run `uv run pytest tests/test_selection_cash_quality.py -q`；Expected：缺接口失败。
- [ ] 写有状态的计算：缺受限资金只让依赖它的指标缺失，FCF收益率仍可计算；现金<受限资金等冲突不裁剪成0。主要算式：

```python
usable_cash = cash - restricted_cash
net_debt = debt - usable_cash
debt_burden = max(net_debt, 0) / fcf
net_cash_yield = max(-net_debt, 0) / mcap
fcf_yield = fcf / mcap
```

上述算式仅在各自前置验证通过后执行：mcap>0、fcf>0用于偿债、现金/受限/债务非负。同日市值来源缺失不使用年末shares补算。银行返回not_applicable而非0。

- [ ] 重跑，补负FCF、债务为正、受限资金缺失、上市后增发导致旧年末股本失真、金融分支；Expected：全部通过。
- [ ] Commit `fix: separate usable net cash from debt service ranking`。

## T07：冻结季度事实、TTM与当期状态

**Files:** Create `discovery/fundamentals.py`, `tests/test_selection_periods.py`, `tests/test_selection_snapshot.py`; Modify `discovery/company_payload.py`, `discovery/snapshot.py`, `market/types.py`, `market/eastmoney.py`, `jobs/discovery_data.py`, `jobs/discovery.py`, `research/evaluate.py`。

**Interfaces:** `ttm_amount(rows:Sequence[EvidenceRecord],field:str,end_period:str)->MetricResult`；`quarter_amount(rows:Sequence[EvidenceRecord],field:str,end_period:str)->MetricResult`；`operating_state(snapshot:FundamentalSnapshot,policy:Mapping[str,object])->OperatingResult`；`freeze_fundamentals(records:Sequence[EvidenceRecord],actions:Sequence[CapitalEvent],coverage:Sequence[CoverageWindow],*,code:str,cutoff_at:datetime,current_mcap:Decimal|None)->FundamentalSnapshot`。

- [ ] 写失败测试TTM及后发信息隔离：

```python
from decimal import Decimal
from discovery.fundamentals import ttm_amount
from tests.selection_helpers import evidence

def test_deducted_ttm_uses_comparable_cumulative_reports():
    rows = (evidence('2025',deducted_net_income='100'),
            evidence('2025H1',deducted_net_income='40'),
            evidence('2026H1',deducted_net_income='50'))
    assert ttm_amount(rows,'deducted_net_income','2026H1').value == Decimal('110')
```

- [ ] Run `uv run pytest tests/test_selection_periods.py tests/test_selection_snapshot.py -q`；Expected：新接口失败。
- [ ] 实现期间映射（年度/一季/半年/三季）与合并口径校验。一个字段缺失不能靠另一利润字段顶替。把年度与季度数组分开冻结，schema升4，序列化/校验/哈希/重放四处一起更新。

```python
def required_ttm_periods(end_period):
    year = int(end_period[:4])
    suffix = end_period[4:]
    if suffix == '':
        return (end_period,)
    if suffix not in {'Q1','H1','Q3'}:
        raise ValueError('invalid cumulative report period')
    return (str(year - 1), end_period, f'{year - 1}{suffix}')
```

`operating_state`严格按设计8.2四态，政策含period_due_dates与profit_drop=-0.15；披露日历必须来自版本化官方日历，不能按系统当前月猜已披露。当前真实查到的季度使用cutoff筛选，新增实时Financial只用于另一次研究刷新。

- [ ] 删除竞争路径的实时Financial查询，将extra_risk_flags改由frozen operating result派生并持久化到实际排名审计/案例卡；冻结后改Financial/追加新公告，旧批次排名及指纹必须完全相同。
- [ ] 运行新旧冻结与回放回归：

```powershell
uv run pytest tests/test_selection_periods.py tests/test_selection_snapshot.py tests/test_discovery_data.py tests/test_replay.py -q
```

Expected：全部通过。补累计转单季、春节季节性同比、前期零/亏损、合并范围改变、date_only、报表重述和缺TTM端点。
- [ ] Commit `feat: freeze quarterly fundamentals and calculate deducted TTM`。

## T08：资本回报与回款影子指标

**Files:** Create `tests/test_selection_capital_return.py`, `tests/test_selection_collection.py`; Modify `discovery/fundamentals.py`, `discovery/field_dictionary.py`, `market/eastmoney.py`, `discovery/tracks/garp.py`。

**Interfaces:** `roic_gross(*,ebit:Decimal,tax_rate:Decimal,ic_begin:Decimal,ic_end:Decimal)->MetricResult`；`collection_signals(snapshot:FundamentalSnapshot,policy:Mapping[str,object])->tuple[str,...]`；`capital_return(snapshot:FundamentalSnapshot)->MetricResult`计算5年均值，需要6个年末IC。

- [ ] 写失败测试：ROIC可手算、ROE再高也不补缺失的EBIT。

```python
from decimal import Decimal as D
from discovery.fundamentals import roic_gross

def test_gross_roic_uses_average_invested_capital():
    result = roic_gross(ebit=D('20'),tax_rate=D('0.25'),ic_begin=D('90'),ic_end=D('110'))
    assert result.value == D('0.15')
```

- [ ] Run `uv run pytest tests/test_selection_capital_return.py tests/test_selection_collection.py -q`；Expected：新接口失败。
- [ ] 实现税率有效域和三年回退、正IC、金融不适用；单位decimal ROIC在界面乘100，不与既有百分数ROE直接比较。核验interest_expense及minority口径，缺独立利息不能用财务费用。

```python
def nopat_ratio(ebit, tax_rate, ic_begin, ic_end):
    if not 0 <= tax_rate <= 1 or ic_begin <= 0 or ic_end <= 0:
        raise ValueError('capital return inputs out of range')
    return ebit * (1 - tax_rate) / ((ic_begin + ic_end) / 2)
```

应收/合同资产占收入升幅≥0.05且现金转换下降、受限资金占现金≥0.20只输出shadow标记；无数据输出独立missing，不制造“未触发”。
- [ ] 重跑，补并购导致合并口径跳变、少数股东权益、税前亏损、0税率、五年不足、银行、缺应收或合同资产；Expected：全部通过。
- [ ] Commit `feat: add auditable capital return and collection diagnostics`。

## T09：统一接入三轨v8与可解释结果

**Files:** Modify `discovery/builds.py`, `discovery/quality_contract.py`, `discovery/rulers.py`, `discovery/tracks/quality_discount.py`, `discovery/tracks/deep_value.py`, `discovery/tracks/garp.py`, `discovery/evidence.py`, `discovery/summaries.py`, `discovery/diff.py`, `jobs/discovery.py`, `scripts/calibrate_discovery.py`; Create `tests/test_selection_v8.py`, `docs/discovery-calibration/v8/quality-discount.json`, `docs/discovery-calibration/v8/deep-value.json`, `docs/discovery-calibration/v8/garp.json`。

**Interfaces:** 维持现有 `classify(FrozenCompanyInput,TrackRuler)->TrackVerdict`；扩展尺子 `ranking_contract/operating_policy/metric_contract` 并纳入digest；新增指标按单一注册表进入loader、窗口、公式、门槛、缺失策略和证据。

- [ ] 写失败测试：使用现有各轨payload/company fixture构造普通PE=10、扣非PE=50且扣非增长20%的公司，GARP应因扣非估值失败；同普通PE历史不重算为扣非历史。

```python
from decimal import Decimal as D
from discovery.fundamentals import deducted_peg

def test_one_off_income_cannot_lower_deducted_peg():
    assert deducted_peg(mcap=D('1000'),deducted_ttm=D('20'),growth=D('0.20')).value == D('2.5')
```

- [ ] Run `uv run pytest tests/test_selection_v8.py -q`；Expected：新口径未接入导致失败。
- [ ] 在fundamentals导出 `deducted_peg(*,mcap:Decimal,deducted_ttm:Decimal,growth:Decimal)->MetricResult`，非正输入返回missing/不满足门槛的明确原因；实现算式并接入GARP、优质折价PEG放行与绝对收益率。

```python
from decimal import Decimal

def peg_value(mcap, deducted_ttm, growth):
    return (mcap / deducted_ttm) / (min(growth, Decimal('0.25')) * 100)
```

普通历史PE分位和扣非当前PE采用不同key与label。GARP资本门槛改用小数阈值roic_gross_min（保守0.12/基准0.10/宽松0.08），不再用百分数return_on_capital_min与小数结果比较；测试0.10达标、0.099不达标。新排序使用设计第6节完整分支表；同一frozen metrics既用于排序又用于案例卡，不再丢失竞争阶段新增风险。doc中v7校准产物不改写为v8，生成独立版本文件。
- [ ] Run `uv run pytest tests/test_selection_v8.py tests/test_discovery_tracks_quality.py tests/test_discovery_tracks_deep_value.py tests/test_discovery_tracks_garp.py tests/test_discovery_rulers.py tests/test_discovery_evidence.py tests/test_discovery_diff.py -q`；Expected：全部通过。旧结果只读仍用旧原文，v8候选不进入现行库，生产激活留T19。
- [ ] Commit `feat: wire evidence-based metrics into discovery v8`。

## T10：股东回报兑现与价值实现路径

**Files:** Create `research/shareholder_return.py`, `tests/test_shareholder_return.py`; Modify `market/evidence.py`, `market/corporate_actions.py`, `research/evaluate.py`。

**Interfaces:** `shareholder_metrics(records:Sequence[EvidenceRecord],*,mcap:Decimal|None,fcf:Decimal|None,deducted_profit:Decimal|None,coverage:Sequence[CoverageWindow]=())->dict[str,MetricResult]`；`latest_progress(records:Sequence[EvidenceRecord])->tuple[EvidenceRecord,...]` 按plan_id/period选择最后有效累计进度，按实际成交明细时按trade_id去重，二者不能叠加。

- [ ] 写失败测试：两条回购进度分别累计100和150，实际回购为150；只发布额度不算已回报。

```python
from dataclasses import replace
from decimal import Decimal as D
from tests.selection_helpers import evidence
from research.shareholder_return import shareholder_metrics

def test_announced_buyback_is_not_cash_return():
    row = replace(evidence('2026',plan_id='p',stage='proposed',amount='100'),kind='buyback')
    result = shareholder_metrics((row,),mcap=D('1000'),fcf=D('100'),deducted_profit=D('100'))
    assert result['buyback_executed'].value is None
```

- [ ] Run `uv run pytest tests/test_shareholder_return.py -q`；Expected：新模块失败。
- [ ] 实现按kind/stage取数，执行记录完整覆盖时才汇总；计划和实际分别呈现。现金分红采用paid，回购executed，注销cancelled；没有执行记录且覆盖未知是missing，完整覆盖证明无执行才为0。

```python
def latest_progress(records):
    latest = {}
    for row in records:
        key = (row.kind, row.values['plan_id'])
        if key not in latest or row.published_at > latest[key].published_at:
            latest[key] = row
    return tuple(latest.values())
```

累计进度递减必须有supersedes修订关联，否则标untrusted；股权融资与回购净额并列显示。研究输出增加realization_path及证据/失效条件；unknown不会强行淘汰研究对象，但不能自动确认安全边际。
- [ ] 重跑，补分红跨年支付、撤回预案、回购用于激励、完成回购未注销、FCF负值、银行不套FCF、执行金额缺失；Expected：全部通过。
- [ ] Commit `feat: distinguish shareholder return plans from execution`。

## T11：证据化研究与确定性信息等级

**Files:** Create `research/evidence.py`, `tests/test_research_evidence.py`; Modify `research/evaluate.py`, `research/search.py`, `jobs/research.py`, `app/models.py`, `app/db.py`。

**Interfaces:** `validate_claims(claims:Sequence[Mapping[str,object]],records:Sequence[EvidenceRecord])->tuple[str,...]` 返回错误码；`research_grade(*,financial_complete:bool,valuation_complete:bool,claim_coverage:Sequence[bool])->str`；`freeze_research_context(records:Sequence[EvidenceRecord],assumption_id:int|None,cutoff_at:datetime)->Mapping[str,object]`。assumption_id引用设计5.4的整数主键，未提供时估值只能watch。

- [ ] 写失败测试：引用不存在/未来证据、claim缺反证说明或失效条件、模型自称A级但财务不完整。

```python
from research.evidence import research_grade

def test_model_confidence_cannot_upgrade_missing_facts():
    assert research_grade(financial_complete=False,valuation_complete=True,
                          claim_coverage=[True]*6) == 'C'
    assert research_grade(financial_complete=True,valuation_complete=True,
                          claim_coverage=[True,True,False,True,True,True]) == 'B'
```

- [ ] Run `uv run pytest tests/test_research_evidence.py tests/test_evaluate.py -q`；Expected：新合同测试失败。
- [ ] 在模型输入给证据ID、来源和原文摘录，模型输出claim/evidence_ids/counterevidence_ids/falsification_condition/checked_at/status；反证为空时必须有“未找到反证”的查询范围记录，不能装作不存在反证。独立校验代码覆盖模型data_grade。

```python
def research_grade(*, financial_complete, valuation_complete, claim_coverage):
    if not financial_complete or not valuation_complete:
        return 'C'
    return 'A' if len(claim_coverage) == 6 and all(claim_coverage) else 'B'
```

关键缺失把pass降gray；fail保持fail。旧研究原文保持不变，页面显示legacy_evidence_contract，不用新规则回写旧分数。搜索摘要仅作线索，官方材料进入证据后才支持财务判断。
- [ ] 重跑，补两次研究同公司不同证据cutoff、源文件变更hash不一致、提示注入不能添加不存在的财务事实；Expected：全部通过。
- [ ] Commit `feat: ground research claims in frozen evidence`。

## T12：确定性估值情景与价格带

**Files:** Create `research/valuation.py`, `tests/test_research_valuation.py`; Modify `research/evaluate.py`, `research/thesis.py`, `app/selection_models.py`。

**Interfaces:** `ValuationAssumptions(method:str,metric_per_share:Decimal,growth:Decimal,multiples:tuple[Decimal,Decimal,Decimal],haircuts:tuple[Decimal,Decimal,Decimal],horizon_years:int,evidence_ids:tuple[str,...])`；`price_bands(assumptions:ValuationAssumptions)->tuple[Decimal,Decimal,Decimal]`；`valuation_report(assumptions:ValuationAssumptions,current_price:Decimal|None)->Mapping[str,object]`。前置证据校验失败在外层返回watch，纯函数非法输入抛ValueError。设计中展开的normalized_metric_per_share/earnings_growth等持久化字段，在此DTO映射为metric_per_share/growth与三元素tuple；唯一转换函数写在本模块，不在调用端各自拼装。

- [ ] 写失败测试可手算结果及模型不可改写：

```python
from decimal import Decimal as D
from research.valuation import ValuationAssumptions, price_bands

def test_price_bands_are_reproducible_from_assumptions():
    a = ValuationAssumptions('pe',D('2'),D('0.1'),(D('10'),D('12'),D('15')),
                            (D('0.2'),D('0.1'),D('0')),1,('e',))
    assert price_bands(a) == (D('17.6'),D('23.76'),D('33'))
```

- [ ] Run `uv run pytest tests/test_research_valuation.py tests/test_thesis.py -q`；Expected：新增测试失败。
- [ ] 估值假设表追加写，规范化假设/来源摘要作为版本ID；纯计算禁止访问LLM。核心：

```python
def price_bands(a):
    if a.method not in {'pe','pb'} or a.metric_per_share <= 0 or a.growth <= -1:
        raise ValueError('invalid valuation metric')
    if a.horizon_years < 1 or not a.evidence_ids:
        raise ValueError('missing valuation basis')
    if any(m <= 0 for m in a.multiples) or any(not 0 <= h < 1 for h in a.haircuts):
        raise ValueError('invalid valuation assumptions')
    result = tuple(a.metric_per_share*(1+a.growth)**a.horizon_years*m*(1-h)
                   for m,h in zip(a.multiples,a.haircuts,strict=True))
    if len(result) != 3 or not result[0] < result[1] < result[2]:
        raise ValueError('invalid price band order')
    return result
```

外层新增悲观价与增长/倍数敏感性表；缺假设、失效证据、非正扣非、银行净资产口径不明时watch。维持旧字段映射但新UI采用设计中的明确标签。国债利差读取来源与日期，超过30天标待核，不能继续展示旧收益率形成的判断。
- [ ] 重跑，补有限数校验、送转后的每股基础、PB法、倍数顺序错误、缺证据、模型返回不同价格、旧报告读取；Expected：全部通过。
- [ ] Commit `feat: calculate valuation bands from versioned assumptions`。

## T13：隔离模型标签与规则激活

**Files:** Modify `scripts/evolve_rulers.py`, `tests/test_evolve_rulers.py`; Create `tests/test_rule_validation_boundary.py`。

**Interfaces:** 保留evolve入口及既有默认dry-run；新增 `proposal_kind` 和 `validation_report` 输出。`activation_eligibility(*,proposal_kind:str,evidence:Mapping[str,object])->tuple[bool,str|None]`；research_hypothesis不能激活，data_error_fix需确定性验收摘要，validated_experiment需独立时间外验证清单。

- [ ] 写失败测试：两份/二十份模型fail都不能激活，gray不等于投资成功。

```python
from scripts.evolve_rulers import activation_eligibility

def test_llm_labels_never_suffice_for_activation():
    ok, reason = activation_eligibility(proposal_kind='research_hypothesis',
                                       evidence={'fail_count':20,'gray_count':50})
    assert not ok
    assert reason == 'independent_validation_required'
```

- [ ] Run `uv run pytest tests/test_rule_validation_boundary.py tests/test_evolve_rulers.py -q`；Expected：旧“两个样本确认”测试需因新业务合同改为拒绝。
- [ ] 删除模型fail→false_positive自动真值映射；保留名称明确的research_disagreement。`--apply`拒绝时退出码2并不写库，输出待验证假设及证据缺口。

```python
def model_activation_guard(proposal_kind):
    if proposal_kind == 'research_hypothesis':
        return False, 'independent_validation_required'
    return True, None
```

上段只承担模型来源的第一道拒绝；完整eligibility继续核对验证报告哈希、预注册时间、未变更基准、成熟期限、数据覆盖、time-out-of-sample和发布理由。不存在“样本数够了自动有效”。
- [ ] 重跑并对临时数据库断言失败前后ruler_versions完全一致；Expected：全部通过。
- [ ] Commit `fix: keep research labels out of automatic ruler activation`。

## T14：预注册实验与纸面队列

**Files:** Create `tracking/__init__.py`, `tracking/contracts.py`, `tracking/cohorts.py`, `tests/test_tracking_cohorts.py`; Modify `app/selection_models.py`, `app/db.py`, `pyproject.toml`。

**Interfaces:** `default_tracking_config()->dict[str,object]`；`register_cohort(session:Session,*,run_id:int,track:str,variant_id:str,config:Mapping[str,object],registered_at:datetime)->int`；`cohort_manifest(session,cohort_id:int)->Mapping[str,object]`。manifest冻结members/code/weight、发布时间、cutoff、source run hash、config hash。

- [ ] 写失败测试：注册幂等、历史名单不允许标prospective、改成员/基准/费用失败、全名单与入选组成员从当时持久结果取得。

```python
from tracking.contracts import default_tracking_config

def test_default_experiment_declares_execution_and_maturity():
    config = default_tracking_config()
    assert config['horizons_months'] == [3,6,12]
    assert config['entry'] == 'next_full_session_open'
    assert config['unfilled'] == 'retain_cash'
    assert config['round_lots'] is False
```

- [ ] Run `uv run pytest tests/test_tracking_cohorts.py -q`；Expected：新接口失败。
- [ ] 定义配置并对其canonical JSON取hash；冻结的值为：

```python
def default_tracking_config():
    return {
        'version':'paper-v1', 'horizons_months':[3,6,12],
        'entry':'next_full_session_open', 'max_entry_sessions':5,
        'unfilled':'retain_cash', 'round_lots':False, 'cash_rate':'0',
        'cost_bps_each_side':'15', 'sensitivity_bps':['0','15','30','50'],
        'primary_benchmark':'CSI300_TOTAL_RETURN',
        'secondary_benchmark':'CSI500_TOTAL_RETURN',
        'rights_subscription':False,
        'dividend_policy':'ex_date_receivable_pay_date_cash',
    }
```

`strategy_experiments`保存配置与注册时间；`strategy_cohorts`保存run/track/variant和状态；`strategy_members`使用(cohort,code)唯一，weight均分原始成员，未成交仍保留。批次发布后建队列在同一服务事务写发布时间与登记时间；HTTP不接受注册时间覆盖，服务使用注入时钟。旧run导入统一retrospective_observation。未取得未来行情也可成功登记pending。变体ID固定full_selected/no_operating_selected/all_passed，三组成员均在发布时冻结；无当期排序变体只移除经营风险层，保留其他风险、相同门槛和行业名额。
- [ ] 重跑，补跨轨重复去重只在汇总发生、竞争挤出不变卖出、重复POST同指纹同ID、不同config明确不同实验；Expected：全部通过。
- [ ] Commit `feat: register immutable forward selection cohorts`。

## T15：事件账本、交易限制与收益

**Files:** Create `tracking/market.py`, `tracking/ledger.py`, `tracking/metrics.py`, `tests/test_tracking_ledger.py`, `tests/test_tracking_metrics.py`, `tests/fixtures/selection/market-events.json`; Modify `app/selection_models.py`。

**Interfaces:** `simulate_cohort(manifest:Mapping[str,object],events:Sequence[Mapping[str,object]],as_of:date)->Mapping[str,object]`；`equity_return(initial:Decimal,ending:Decimal)->Decimal`；`max_drawdown(equity:Sequence[Decimal])->Decimal`。输出按3/6/12个月分别键控的ledger/equity_curve/entry_coverage/exit_status/gross_return/net_return/horizon_status，不满足观察期限时horizon_return=None；各期限独立持仓，短期退出不影响长期路径。

事件规范：必需event_id/code/type/effective_at/observed_at/source_id；bar额外open/close/volume/suspended/open_limit_up/open_limit_down；split额外factor；dividend_ex额外cash_per_share/pay_date；dividend_paid关联原ex event_id；delisting额外settlement_status/cash_per_share；benchmark额外index_id/total_return_level。所有数值字符串，时间UTC；事件ID唯一，冲突拒绝。

- [ ] 写失败测试：分红除权前后总权益守恒、送转守恒、涨停不买、跌停不卖、未成交保留现金、T+1、退市未决、分红不双算。

```python
from decimal import Decimal as D
from tracking.ledger import dividend_receivable
from tracking.metrics import max_drawdown

def test_dividend_receivable_preserves_ex_date_equity():
    assert D('100')*D('9') + dividend_receivable(D('100'),D('1')) == D('1000')

def test_drawdown_uses_the_entire_daily_equity_path():
    assert max_drawdown((D('100'),D('120'),D('90'),D('110'))) == D('0.25')
```

- [ ] Run `uv run pytest tests/test_tracking_ledger.py tests/test_tracking_metrics.py -q`；Expected：新接口失败。
- [ ] 实现事件驱动账本，现金/股数/应收分别存，除权登记应收，支付只转资产类别。公开纯函数 `dividend_receivable(shares:Decimal,cash_per_share:Decimal)->Decimal` 即shares×cash_per_share；禁止在支付日再加一次收益。

```python
from decimal import Decimal

def max_drawdown(equity):
    if not equity or equity[0] <= 0:
        raise ValueError('invalid equity curve')
    peak, worst = equity[0], Decimal('0')
    for value in equity:
        if value < 0:
            raise ValueError('negative portfolio equity')
        peak = max(peak, value)
        worst = max(worst, (peak-value)/peak)
    return worst
```

日历月加法使用calendar.monthrange截齐月底，再查交易日历。入场等待5个交易日，不是5个自然日。费用从现金扣，入场分数股=(分配现金)/(open×(1+cost))。未决退市显示最后已核价和零回收压力情景两套值，completed收益保持None。退出待成交期间继续按市场事件估值，不提前锁定卖出价。
- [ ] 重跑并加先分红后送转/同日顺序、退出后分红应收、配股不认购、交易日历缺口、benchmark缺失、价格与复权因子冲突、零持仓、只买不卖费用、3个月退出不改变6/12个月路径、同一事件重放幂等；Expected：全部通过。
- [ ] Commit `feat: calculate paper returns with dividends and execution constraints`。

## T16：历史导入、变体比较与跟踪任务

**Files:** Create `tracking/importer.py`, `tracking/evaluation.py`, `jobs/tracking.py`, `scripts/track_selection.py`, `tests/test_tracking_import.py`, `tests/test_tracking_evaluation.py`, `tests/test_tracking_job.py`; Modify `jobs/lock.py`。

**Interfaces:** `validate_history_bundle(bundle:Mapping[str,object])->tuple[str,...]`；`compare_cohorts(observations:Sequence[Mapping[str,object]])->Mapping[str,object]`；`refresh_tracking(session_factory,provider,*,as_of:date)->Mapping[str,object]`。provider公开 `fetch_events(codes:Sequence[str],start:date,end:date)->tuple[Mapping[str,object],...]`，基准代码也通过该接口查询。

- [ ] 写失败测试：只有现存股票+最新报表的包不得通过历史回测资格，未到期不显示期限收益，缺基准不能给相对收益。

```python
from tracking.importer import validate_history_bundle

def test_current_financials_cannot_impersonate_historical_information():
    errors = validate_history_bundle({'schema_version':1,'point_in_time_verified':False})
    assert 'historical_evidence_unavailable' in errors
```

- [ ] Run `uv run pytest tests/test_tracking_import.py tests/test_tracking_evaluation.py tests/test_tracking_job.py -q`；Expected：新接口失败。
- [ ] JSON包schema1要求universe_intervals/report_versions/trading_calendar/market_events/benchmark_series/source_manifest，每块有sha256、coverage与来源。point_in_time_verified不能靠布尔值自证，逐条验证available_at、archive_verified、修订链和证券进退市区间。

```python
REQUIRED_HISTORY_PARTS = {
    'universe_intervals','report_versions','trading_calendar',
    'market_events','benchmark_series','source_manifest',
}

def missing_history_parts(bundle):
    return tuple(sorted(REQUIRED_HISTORY_PARTS - bundle.keys()))
```

导入默认dry-run并报告缺口；完整包才进入T15同一引擎。比较固定三变体与主/辅基准，按轨道/版本/登记月份输出覆盖、回撤、收益和成本，重叠样本只作描述，不声称独立显著性。CLI合同：

```powershell
uv run python -m scripts.track_selection --db PATH --register-run RUN_ID --dry-run
uv run python -m scripts.track_selection --db PATH --refresh --as-of YYYY-MM-DD
uv run python -m scripts.track_selection --db PATH --import-history PATH --dry-run
uv run python -m scripts.track_selection --db PATH --report --format json
```

PATH/RUN_ID/YYYY-MM-DD是CLI参数示意，不在生产库直接复制执行。任务锁类型tracking，租约续期/过期恢复复用jobs.lock；HTTP取数事务外、短事务追加事件与观测，失败可以幂等续跑，不持锁等待数小时。
- [ ] 重跑，加历史幸存者缺口、后发重述、验证来源伪造、重复入库、网络中断续跑、临时失败不擦除旧观测、三个月未成熟；Expected：全部通过。
- [ ] Commit `feat: evaluate registered variants with point-in-time data checks`。

## T17：发现、研究与跟踪界面集成

**Files:** Create `app/tracking_routes.py`, `web/src/views/TrackingView.vue`, `web/src/views/trackingViewModel.ts`, `web/src/views/__tests__/trackingViewModel.test.ts`, `tests/test_tracking_routes.py`; Modify `app/main.py`, `app/discovery_routes.py`, `web/src/router/index.ts`, `web/src/App.vue`, `web/src/views/DiscoveryView.vue`, `web/src/views/discoveryViewModel.ts`, `web/src/views/ResearchView.vue`, `web/package.json`。

**Interfaces:** `create_tracking_router()->APIRouter`，prefix=/api/tracking；state.session_factory和tracking provider从app.state注入，测试使用假provider。`trackingViewModel.ts`导出 `formatHorizon(row:{status:string;net_return:string|null}):string`，未来未到期显示“尚未到期”，不是“0%”。

- [ ] 写失败测试空队列和未知状态：

```python
from fastapi.testclient import TestClient
from app.main import create_app
from market.quotes import FakeQuoteClient

def test_tracking_starts_with_an_empty_collection(tmp_path):
    app = create_app(db_path=tmp_path/'test.db',env_path=tmp_path/'.env',quote_client=FakeQuoteClient())
    with TestClient(app) as client:
        response = client.get('/api/tracking/cohorts')
        assert response.status_code == 200
        assert response.json()['items'] == []
```

- [ ] Run `uv run pytest tests/test_tracking_routes.py -q`；Expected：路由不存在404失败。
- [ ] 实现设计第12节五条API和HTTP错误映射，POST幂等校验；GET无写操作。发现卡片复用实际存下来的risk/score audit，不在前端重算。研究价格带从计算结果读，显示证据缺口；跟踪页先呈现状态和覆盖率。

```typescript
export function formatHorizon(row: {status: string; net_return: string | null}): string {
  if (row.status === 'pending_maturity') return '尚未到期'
  if (row.status === 'exit_pending') return '等待退出成交'
  if (row.net_return === null) return '数据不足'
  return `${(Number(row.net_return) * 100).toFixed(2)}%`
}
```

把trackingViewModel.test.ts加入package.json的test:view-model；断言pending+null不是0%、legacy卡片不显示新结论、名额排除与门槛失败文案不同。不能只新增未被test脚本执行的测试文件。
- [ ] Run `uv run pytest tests/test_tracking_routes.py tests/test_discovery.py tests/test_research.py -q`、`npm --prefix web run test:view-model`、`npm --prefix web run build`；Expected：均通过。用本机临时库启动后检查空态/不足/完整/未到期/退出待成交及窄屏；页面数值与API fixture一致。
- [ ] Commit `feat: expose selection evidence and independent tracking`。

## T18：组合共同风险与机会成本

**Files:** Create `ledger/exposure.py`, `tests/test_portfolio_exposure.py`; Modify `research/thesis.py`, `review/run.py`, `web/src/views/PositionsView.vue`, `app/selection_models.py`。

**Interfaces:** `ExposurePolicy(limits:Mapping[str,Decimal],unknown_limit:Decimal|None)`；`exposure_totals(weights:Mapping[str,Decimal],labels:Mapping[str,tuple[str,...]])->dict[str,Decimal]`；`check_proposed_exposure(current_weights:Mapping[str,Decimal],delta_weights:Mapping[str,Decimal],labels:Mapping[str,tuple[str,...]],policy:ExposurePolicy)->Mapping[str,object]`；`proposal_priority(verdict:str,operating_state:str,grade:str)->str`。

- [ ] 写失败测试：跨申万行业同地产风险仍累加，多标签不均摊，fail比冻结优先。

```python
from decimal import Decimal as D
from ledger.exposure import exposure_totals, proposal_priority

def test_shared_demand_is_counted_across_industries():
    result = exposure_totals({'a':D('.10'),'b':D('.15')},
                             {'a':('property',),'b':('property','exports')})
    assert result['property'] == D('.25')
    assert result['exports'] == D('.15')
    assert proposal_priority('fail','deteriorating','C') == 'exit_review'
```

- [ ] Run `uv run pytest tests/test_portfolio_exposure.py tests/test_thesis.py -q`；Expected：新增合同失败。
- [ ] 映射版本存证据ID与有效时点；未知股票权重单列unknown。不设上限时不制造超限标志；配置上限时在建议后组合计算，超限只限制新增建议，不修改账本交易。

```python
from decimal import Decimal

def exposure_totals(weights, labels):
    result = {'unknown':Decimal('0')}
    for code, weight in weights.items():
        tags = set(labels.get(code, ()))
        if not tags:
            result['unknown'] += weight
        for tag in tags:
            result[tag] = result.get(tag, Decimal('0')) + weight
    return result
```

从research.thesis.dynamic_sizing取出优先级判断，fail/skip先于drift_freeze，C/unknown/deteriorating禁新增；核心/卫星上限仍为现有15%/8%。备忘录补同日期宽基ETF估值/费用/风险假设对照，无法形成可复算收益时不展示预期超额收益数字。
- [ ] 重跑，加加仓后超限、减仓不受新增约束、标签修订不回写旧建议、未知占比、未设置上限、股权/ETF重复风险与负权重非法；Expected：全部通过。
- [ ] Commit `feat: constrain proposed positions by shared economic exposure`。

## T19：迁移、数据就绪、发布和全量验收

**Files:** Create `scripts/release_selection_v8.py`, `tests/test_selection_migration.py`, `tests/test_selection_release.py`, `tests/test_selection_acceptance.py`; Modify `app/db.py`, `discovery/builds.py`, `discovery/quality_contract.py`, `docs/stock-selection-strategies.md`, `README.md`, `README.en.md`。

**Interfaces:** `release_report(session:Session,*,fill_id:int)->Mapping[str,object]` 返回coverage/semantic_changes/acceptance/eligible；CLI `--db PATH --fill-id ID --dry-run`默认只读报告，`--apply --reason TEXT`才在一个事务追加三轨尺子并验证当前构建一致。默认不调用自动进化的模型标签路径。

- [ ] 写失败测试：旧fixture迁移两次不变、旧冻结hash不变、一个轨道激活失败全部回滚、字段覆盖不足阻止发布、旧程序不写新schema。

```python
from app.db import init_db
from sqlalchemy import text

def test_selection_migration_is_idempotent(tmp_path):
    path = tmp_path/'migration.db'
    first = init_db(path)
    with first.connect() as conn:
        before = conn.execute(text('SELECT COUNT(*) FROM ruler_versions')).scalar_one()
    first.dispose()
    second = init_db(path)
    with second.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM ruler_versions')).scalar_one() == before
    second.dispose()
```

- [ ] Run `uv run pytest tests/test_selection_migration.py tests/test_selection_release.py tests/test_selection_acceptance.py -q`；Expected：新迁移/门禁未实现失败。
- [ ] 实现release_report按适用分支计算可信覆盖（共同95%、新增关键字段90%），逐行业列数，不把不适用金融类算缺失；候选数量不是发布指标。新增schema4 fill必须独立抓取；旧payload不自动补字段。执行用SQLite backup API建立一致备份，先核实绝对路径在预期备份目录，校验能打开与关键表计数。

```python
from decimal import Decimal

def coverage_ready(trusted, applicable):
    return applicable > 0 and Decimal(trusted)/Decimal(applicable) >= Decimal('0.90')
```

不存在适用样本的分支显示not_applicable，由发布报告单独列出，不能把0/0当100%。三个尺子和release元数据一起提交；v7退休只读。回退不改原记录，以构建选择和追加发布记录完成，未知版本进入只读模式。
- [ ] 更新两种语言README和策略总览：说明新排名、经济稀释、扣非口径、影子信号、纸面模拟限制与旧批次边界。新增实际字段来源核验报告要包含真实抽查证据，未通过覆盖时准确标“生产尚未就绪”。
- [ ] 运行最终检查，命令如下。Expected：后端全套、前端视图模型、类型检查/构建均通过，diff无空白错误；失败时停止发布并报告原始原因，不降低测试范围换取绿色。

```powershell
uv run pytest -q
npm --prefix web run test:view-model
npm --prefix web run build
git diff --check
```

- [ ] 用临时库完成以下端到端验收：同日市场/不同公告cutoff→三轨→排名审计→证据化研究→价格带→建议后暴露→登记队列→模拟分红停牌→未到期/到期→基准比较→模型标签激活被拒绝。逐项对照设计R01—R16记录命令、fixture与结果。
- [ ] Commit `feat: release verified selection v8 with immutable history`。开发任务完成报告必须分列软件验收、数据源就绪、生产启用、期限成熟四个状态。此提交名不允许替代实际发布报告；没有执行--apply就不得说生产已启用。

## 完成审计与交接

### 文档交付检查（本次执行）

- 设计R01—R16各有任务覆盖，T01—T19各有Files、Interfaces、失败测试、运行命令、实现方向、通过检查与提交点。
- 所有引用的既有路径已核查，新增路径显式标Create；测试文件在相关任务创建后才执行。
- 冻结、公告时点、股本基础、缺失值、回测执行、分红记账、生产发布的合同互相一致。
- 没有要求工程师凭“类似上一任务”补齐关键接口；未验证数据源写清能力门禁，没有伪装现成可用。
- 文档内的代码样例需语法检查，R/T映射与本地链接需检查；这些检查仅验证文档，不表示软件已实现。

### 实施后交付

开发者交付代码与回归记录、字段来源核验、迁移/回退演练、覆盖报告、v8校准差异、纸面实验注册与样例结果。真实3/6/12个月数据未成熟时保留pending，不能为了完成任务伪造结果。

推荐按任务逐个执行并审核；可以选择Superpowers subagent-driven-development逐任务开发，也可使用executing-plans在当前会话顺序执行。无论采用哪一种，执行者先读设计与本计划，不直接从T04开始跳过数据和时间合同。
