from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from research.evaluate import GATE_NAMES, EvalInput, normalize_structured


def _input(tmp_path: Path) -> EvalInput:
    return EvalInput(
        name="华润三九",
        code="000999.SZ",
        financials=({"period": "2024", "roe": 18, "net_income": 1},),
        price=Decimal("24"),
        book={"positions": [], "cash_balance": 0},
        web_search=False,
        as_of_date="2026-08-31",
        job_id=1,
        reports_dir=tmp_path,
    )


def _rest() -> dict:
    return {
        "mirror_test": ["一", "二", "三", "四", "五"],
        "price_bands": {
            "mode": "watch",
            "aggressive": "",
            "steady": "",
            "conservative": "",
        },
        "memo": {"essence": "主业制药", "risks": "集采", "role": "satellite"},
        "data_grade": "B",
        "as_of": {"date": "2026-08-31", "price": 24},
        "portfolio_fit": {
            "should_enter": "已在当前仓，对照现金再决定。",
            "funding": "不新开仓。",
            "overlap": "与铝业不叠同一逻辑。",
        },
    }


def test_normalize_accepts_gates_object_map(tmp_path: Path) -> None:
    raw = {
        "verdict": "pass",
        "gates": {
            name: {"status": "pass", "reason": f"{name}可核验"} for name in GATE_NAMES
        },
        **_rest(),
    }
    got = normalize_structured(raw, _input(tmp_path))
    assert [row["name"] for row in got["gates"]] == list(GATE_NAMES)
    assert all(row["status"] == "pass" for row in got["gates"])


def test_normalize_accepts_chinese_keys_and_statuses(tmp_path: Path) -> None:
    raw = {
        "结论": "灰色地带",
        "六关": {
            name: {"status": "灰色", "理由": f"{name}需再看"} for name in GATE_NAMES
        },
        "镜子测试": ["一", "二", "三", "四", "五"],
        "价格带": {"策略": "观望"},
        "备忘": {"生意本质": "制药", "主要风险": "集采", "仓位角色": "卫星"},
        "信息丰富度": "B",
        "研究日期": {"date": "2026-08-31"},
        "对照当前仓": {
            "该不该进当前仓": "已持有。",
            "用什么钱买": "不用新钱。",
            "是否叠逻辑": "不叠铝业。",
        },
    }
    got = normalize_structured(raw, _input(tmp_path))
    assert got["verdict"] == "gray"
    assert len(got["gates"]) == 6
    assert got["memo"]["role"] == "satellite"
    assert got["price_bands"]["mode"] == "watch"


def test_normalize_accepts_messy_portfolio_fit(tmp_path: Path) -> None:
    raw = {
        "verdict": "gray",
        "gates": {
            name: {"status": "gray", "reason": f"{name}需再看"} for name in GATE_NAMES
        },
        **_rest(),
    }
    raw["portfolio_fit"] = {
        "该不该进": False,
        "资金来源": "用现金，不动旧仓。",
        "会不会叠逻辑": "与铝业同属资源周期，权重合计要控。",
    }
    got = normalize_structured(raw, _input(tmp_path))
    assert got["portfolio_fit"]["should_enter"] == "否。"
    assert "现金" in got["portfolio_fit"]["funding"]
    assert "铝业" in got["portfolio_fit"]["overlap"]
    raw["portfolio_fit"] = ["不进。", "不用钱。", "叠锂电周期。"]
    got = normalize_structured(raw, _input(tmp_path))
    assert got["portfolio_fit"] == {
        "should_enter": "不进。",
        "funding": "不用钱。",
        "overlap": "叠锂电周期。",
    }


def test_normalize_accepts_hold_role_and_band_and_verdict_phrases(tmp_path: Path) -> None:
    from research.evaluate import _parse_model_json

    raw = {
        "verdict": "通过（有保留）",
        "gates": [
            {"name": f"{name}（再核）", "status": "通过", "reason": f"{name}可核验"}
            for name in GATE_NAMES
        ],
        **_rest(),
    }
    raw["price_bands"] = {"mode": "稳健持有", "aggressive": "", "steady": "", "conservative": ""}
    raw["memo"] = {"essence": "锂盐与资源。", "risks": "周期与锂价。", "role": "继续持有"}
    got = normalize_structured(raw, _input(tmp_path))
    assert got["verdict"] == "pass"
    assert got["price_bands"]["mode"] == "steady"
    assert got["memo"]["role"] == "satellite"
    parsed = _parse_model_json('前言\n{"verdict":"gray"}\n以上。')
    assert parsed == {"verdict": "gray"}
    raw["verdict"] = "未通过"
    got = normalize_structured(raw, _input(tmp_path))
    assert got["verdict"] == "fail"
    raw["verdict"] = "pass"
    raw["price_bands"] = {
        "mode": "steady",
        "便宜买": "8",
        "合理买": "10",
        "停止加仓": "12",
        "明显高估": "16",
    }
    got = normalize_structured(raw, _input(tmp_path))
    assert got["verdict"] == "fail"
    assert got["price_bands"]["aggressive"] == "8"
    assert got["price_bands"]["overvalued"] == "16"


def test_price_between_stop_add_and_overvalued_stays_pass(tmp_path: Path) -> None:
    payload = EvalInput(
        name="招商银行",
        code="600036.SH",
        financials=({"period": "2025", "roe": 13},),
        price=Decimal("41.07"),
        book={"positions": [], "cash_balance": 0},
        web_search=False,
        as_of_date="2026-09-07",
        job_id=1,
        reports_dir=tmp_path,
    )
    raw = {
        "verdict": "pass",
        "gates": [
            {"name": name, "status": "pass", "reason": f"{name}可核验"} for name in GATE_NAMES
        ],
        **_rest(),
    }
    raw["price_bands"] = {
        "mode": "steady",
        "aggressive": "34",
        "steady": "38",
        "conservative": "41",
        "overvalued": "48",
    }
    got = normalize_structured(raw, payload)
    assert got["verdict"] == "pass"


def test_inverted_bands_do_not_flip_pass_to_fail(tmp_path: Path) -> None:
    payload = EvalInput(
        name="华润三九",
        code="000999.SZ",
        financials=({"period": "2025", "roe": 16},),
        price=Decimal("24.61"),
        book={"positions": [], "cash_balance": 0},
        web_search=False,
        as_of_date="2026-09-08",
        job_id=1,
        reports_dir=tmp_path,
    )
    raw = {
        "verdict": "pass",
        "gates": [
            {"name": name, "status": "pass", "reason": f"{name}可核验"} for name in GATE_NAMES
        ],
        **_rest(),
    }
    raw["price_bands"] = {
        "mode": "steady",
        "aggressive": "26.5",
        "steady": "24.61",
        "conservative": "21.0",
        "overvalued": "",
    }
    got = normalize_structured(raw, payload)
    assert got["verdict"] == "pass"


def test_normalize_accepts_colloquial_memo_role(tmp_path: Path) -> None:
    raw = {
        "verdict": "gray",
        "gates": {
            name: {"status": "gray", "reason": f"{name}需再看"} for name in GATE_NAMES
        },
        **_rest(),
    }
    raw["memo"] = {
        "essence": "OTC 感冒药与中药。",
        "risks": ["集采", {"summary": "渠道费用"}],
        "role": "卫星仓",
    }
    got = normalize_structured(raw, _input(tmp_path))
    assert got["memo"]["role"] == "satellite"
    assert "集采" in got["memo"]["risks"]
    assert "渠道费用" in got["memo"]["risks"]


def test_any_fail_gate_makes_verdict_fail(tmp_path: Path) -> None:
    raw = {
        "verdict": "pass",
        "gates": {
            name: {"status": "pass", "reason": f"{name}可核验"} for name in GATE_NAMES
        },
        **_rest(),
    }
    raw["gates"]["安全边际"] = {"status": "fail", "reason": "现价没有折扣"}
    got = normalize_structured(raw, _input(tmp_path))
    assert got["verdict"] == "fail"
    raw["verdict"] = "gray"
    got = normalize_structured(raw, _input(tmp_path))
    assert got["verdict"] == "fail"


def test_gray_gate_cannot_stay_pass(tmp_path: Path) -> None:
    raw = {
        "verdict": "pass",
        "gates": {
            name: {"status": "pass", "reason": f"{name}可核验"} for name in GATE_NAMES
        },
        **_rest(),
    }
    raw["gates"]["好生意"] = {"status": "gray", "reason": "中报量价一起掉"}
    got = normalize_structured(raw, _input(tmp_path))
    assert got["verdict"] == "gray"


def test_valuation_snapshot_derives_pb_and_stays_sufficient(tmp_path: Path) -> None:
    from research.evaluate import _build_prompt, _enrich_valuation, data_insufficient

    payload = EvalInput(
        name="招商银行",
        code="600036.SH",
        financials=(),
        price=Decimal("40.86"),
        book={"positions": [], "cash_balance": 0},
        web_search=False,
        as_of_date="2026-09-01",
        job_id=2,
        reports_dir=tmp_path,
        valuation={"bps": 45.4, "npl_pct": 0.94, "eps": 2.98},
    )
    assert data_insufficient(payload) is True
    snap = _enrich_valuation(payload)
    assert abs(float(snap["pb"]) - 40.86 / 45.4) < 0.01
    prompt = _build_prompt(payload)
    assert "估值快照" in prompt
    assert "每股净资产" in prompt
    assert "45.4" in prompt
    assert "给出价格带：激进 / 稳健 / 保守，或观望。" in prompt
    assert "不要空着" in prompt
    assert "任一关失败则 verdict 必须是 fail" in prompt
    assert "好生意或护城河可以为 gray" in prompt
    assert "不要用灰色地带回避判断" not in prompt
    assert "无法算出任何比率" not in prompt


def test_pe_uses_annual_eps_not_quarterly(tmp_path: Path) -> None:
    from research.evaluate import _build_prompt, _enrich_valuation

    payload = EvalInput(
        name="招商银行",
        code="600036.SH",
        financials=(
            {
                "period": "2026H1",
                "period_type": "interim",
                "roe": 6.2,
                "net_income": 1,
                "eps": 2.98,
            },
            {
                "period": "2025",
                "period_type": "annual",
                "roe": 13.44,
                "net_income": 1,
                "eps": 5.94,
            },
        ),
        price=Decimal("40.86"),
        book={"positions": [], "cash_balance": 0},
        web_search=False,
        as_of_date="2026-09-02",
        job_id=4,
        reports_dir=tmp_path,
        valuation={"bps": 45.4, "eps": 2.98},
    )
    snap = _enrich_valuation(payload)
    assert abs(float(snap["pe"]) - 40.86 / 5.94) < 0.02
    assert snap["eps"] == 5.94
    assert snap["pe_basis"] == "静态（现价/2025年报EPS）"
    prompt = _build_prompt(payload)
    assert "静态市盈率" in prompt
    assert "禁止改用动态" in prompt
    assert "中报与季报只用于看趋势" in prompt
    assert "给出价格带：激进 / 稳健 / 保守，或观望。" in prompt
    assert "pe_mid" not in snap
    assert "中周期对照" not in prompt


def test_no_price_still_insufficient(tmp_path: Path) -> None:
    from research.evaluate import data_insufficient

    payload = EvalInput(
        name="招商银行",
        code="600036.SH",
        financials=({"period": "2024", "roe": 14, "net_income": 1},),
        price=None,
        book={"positions": [], "cash_balance": 0},
        web_search=False,
        as_of_date="2026-09-01",
        job_id=3,
        reports_dir=tmp_path,
        valuation={"bps": 45.4},
    )
    assert data_insufficient(payload) is True


def test_cyclical_adds_mid_cycle_pe_companion(tmp_path: Path) -> None:
    from research.evaluate import _build_prompt, _enrich_valuation

    years = (
        {"period": "2023", "period_type": "annual", "eps": 1.0, "roe": 8, "net_income": 1},
        {"period": "2024", "period_type": "annual", "eps": 3.0, "roe": 12, "net_income": 1},
        {"period": "2025", "period_type": "annual", "eps": 5.0, "roe": 16, "net_income": 1},
        {"period": "2025Q2", "period_type": "quarterly", "eps": 9.0, "roe": 10, "net_income": 1},
    )
    ganfeng = EvalInput(
        name="赣锋锂业",
        code="002460.SZ",
        financials=years,
        price=Decimal("30"),
        book={"positions": [], "cash_balance": 0},
        web_search=False,
        as_of_date="2026-09-02",
        job_id=5,
        reports_dir=tmp_path,
        valuation={"bps": 10, "eps": 5.0},
    )
    snap = _enrich_valuation(ganfeng)
    assert abs(float(snap["pe"]) - 6.0) < 0.02
    assert abs(float(snap["pe_mid"]) - 10.0) < 0.02
    assert snap["pe_mid_basis"] == "中周期对照（现价/3年年报EPS中位数）"
    prompt = _build_prompt(ganfeng)
    assert "中周期对照市盈率" in prompt
    assert "禁止用中周期对照市盈率替代静态市盈率" in prompt
    assert "禁止把中周期对照市盈率写入保守带上沿" in prompt

    coal = EvalInput(
        name="兖矿能源",
        code="600188.SH",
        financials=years,
        price=Decimal("30"),
        book={"positions": [], "cash_balance": 0},
        web_search=False,
        as_of_date="2026-09-02",
        job_id=6,
        reports_dir=tmp_path,
        valuation={"bps": 10, "eps": 5.0},
        industry="煤炭开采",
    )
    assert "pe_mid" in _enrich_valuation(coal)

    bank = EvalInput(
        name="招商银行",
        code="600036.SH",
        financials=years,
        price=Decimal("30"),
        book={"positions": [], "cash_balance": 0},
        web_search=False,
        as_of_date="2026-09-02",
        job_id=7,
        reports_dir=tmp_path,
        valuation={"bps": 10, "eps": 5.0},
        industry="银行",
    )
    bank_snap = _enrich_valuation(bank)
    assert "pe_mid" not in bank_snap
    assert "中周期对照" not in _build_prompt(bank)


def test_search_hits_enter_prompt_and_do_not_replace_financials(tmp_path: Path) -> None:
    from research.evaluate import _build_prompt

    payload = EvalInput(
        name="重庆啤酒",
        code="600132.SH",
        financials=({"period": "2025", "period_type": "annual", "roe": 80, "net_income": 1, "eps": 2.5},),
        price=Decimal("39"),
        book={"positions": [], "cash_balance": 0},
        web_search=True,
        as_of_date="2026-09-02",
        job_id=8,
        reports_dir=tmp_path,
        valuation={"bps": 3.2, "eps": 2.5},
        search_hits=(
            {"title": "中报", "url": "https://example.com/h1", "content": "销量下降 2.87%"},
        ),
    )
    prompt = _build_prompt(payload)
    assert "网页搜索：已开启" in prompt
    assert "销量下降 2.87%" in prompt
    assert "禁止用摘要改写已抓取财务或估值快照里的数字" in prompt
    assert "静态市盈率" in prompt

    off = EvalInput(
        name="重庆啤酒",
        code="600132.SH",
        financials=({"period": "2025", "roe": 80, "net_income": 1},),
        price=Decimal("39"),
        book={"positions": [], "cash_balance": 0},
        web_search=False,
        as_of_date="2026-09-02",
        job_id=9,
        reports_dir=tmp_path,
    )
    assert "未开启（这不代表没有财务）" in _build_prompt(off)


def test_cninfo_notices_enter_prompt(tmp_path: Path) -> None:
    from research.evaluate import _build_prompt, render_markdown

    payload = EvalInput(
        name="重庆啤酒",
        code="600132.SH",
        financials=({"period": "2025", "period_type": "annual", "roe": 80, "net_income": 1, "eps": 2.54},),
        price=Decimal("39.06"),
        book={"positions": [], "cash_balance": 0},
        web_search=False,
        as_of_date="2026-09-02",
        job_id=10,
        reports_dir=tmp_path,
        notices=(
            {
                "date": "2026-08-20",
                "title": "2026年半年度报告：不分配不转增",
                "url": "https://www.cninfo.com.cn/x",
            },
        ),
    )
    prompt = _build_prompt(payload)
    assert "巨潮公告标题" in prompt
    assert "不分配不转增" in prompt
    assert "禁止编造未出现的派息金额" in prompt
    md = render_markdown(
        payload.name,
        {"verdict": "fail", "notices": [dict(payload.notices[0])]},
        web_search=False,
    )
    assert "巨潮公告（仅标题，无正文）" in md
    assert "不分配不转增" in md
    assert "激进：" in md
    assert "稳健：" in md
    assert "保守：" in md
