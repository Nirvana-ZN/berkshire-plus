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
