from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.models import IndexFundRun
from discovery.constants import TREASURY_YIELD, TREASURY_YIELD_DATE, china_market_date
from screen.index_funds import (
    PROXY_MAX_DAYS,
    YIELD_SPREAD_MIN,
    benchmark_treasury,
    build_payload,
    live_product,
    proxy_band,
    valuation_method,
)

logger = logging.getLogger(__name__)
UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "screen" / "index_fund_universe.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_universe(path: Path | None = None) -> dict:
    target = path or UNIVERSE_PATH
    if not target.is_file():
        raise FileNotFoundError(
            "缺少基金名单，请先运行 uv run python -m scripts.build_index_fund_universe"
        )
    return json.loads(target.read_text(encoding="utf-8"))


def collect(
    client,
    universe: dict,
    today: date,
    progress: Callable[[str], None] = lambda _text: None,
) -> dict:
    items = universe["items"]
    danjuan = {item["index_code"]: item for item in client.index_valuations()}
    etf_codes = [i["fund"]["code"] for i in items if i["fund"]["kind"] == "场内"]
    try:
        etf_rows = {row["code"]: row for row in client.etf_list(etf_codes)}
    except TypeError:
        etf_rows = {row["code"]: row for row in client.etf_list()}
    entries: list[dict] = []
    failures = 0
    for n, index in enumerate(items, 1):
        if n % 10 == 0:
            progress(f"{n}/{len(items)}")
        valuation = index.get("valuation") or {}
        entry: dict = {"index": index, "metric": None, "values": {}, "history": [], "error": None}
        try:
            if valuation.get("source") == "danjuan":
                values = danjuan.get(valuation["code"])
                if values is None:
                    raise LookupError("蛋卷已不再提供该指数")
                method = valuation_method(index["index_name"], bool(values.get("pb_flag")))
                metric = "pb" if method == "pb" or values.get("pb_flag") else "pe"
                entry["metric"] = metric
                entry["values"] = values
                entry["history"] = client.valuation_history(valuation["code"], metric)
                if method == "pb":
                    entry["roe_history"] = client.roe_history(valuation["code"])
                elif method == "review":
                    entry["review"] = "高研发、技术迭代或强周期且账面净资产解释力弱，需专门估值复核"
            elif valuation.get("source") == "csindex":
                # csindex's WAF can't take a call per index per refresh.
                # ponytail: PE = anchor PE x price-index change since the anchor,
                # i.e. earnings frozen until the next quarterly universe rebuild.
                # scripts/check_pe_proxy.py measures how often that flips a verdict.
                if "pe_date" not in valuation:
                    raise LookupError("名单缺少中证 PE 锚点，请运行 --valuation-only")
                closes = client.index_closes(valuation["code"])
                anchor = max((day for day in closes if day <= valuation["pe_date"]), default=None)
                if anchor is not None:
                    ratio = closes[max(closes)] / closes[anchor]
                else:
                    returns = client.daily_returns(index["fund"])
                    if not returns or min(returns) > valuation["pe_date"]:
                        raise LookupError("指数行情覆盖不到 PE 锚点日，请重建名单")
                    ratio = Decimal(1)
                    for day, change in returns.items():
                        if day > valuation["pe_date"]:
                            ratio *= 1 + Decimal(str(change)) / 100
                pe = Decimal(str(valuation["pe_last"])) * ratio
                rate, _ = benchmark_treasury(
                    index["index_name"], index.get("category"), Decimal(TREASURY_YIELD)
                )
                effective_treasury = rate if rate is not None else Decimal(TREASURY_YIELD)
                gate_pe = 100 / (effective_treasury + YIELD_SPREAD_MIN)
                entry["metric"] = "pe"
                entry["values"] = {"pe": pe}
                entry["history"] = [Decimal(str(value)) for value in valuation.get("history") or []]
                entry["pe_from"] = valuation["pe_date"]
                age = (today - date.fromisoformat(valuation["pe_date"])).days
                band = proxy_band(age)
                method = valuation_method(index["index_name"])
                if method == "pb":
                    entry["review"] = "周期或金融指数应按 PB 与正常化 ROE 评估，中证只有 PE"
                elif method == "review":
                    entry["review"] = "高研发、技术迭代或强周期且账面净资产解释力弱，需专门估值复核"
                elif age > PROXY_MAX_DAYS:
                    entry["review"] = f"PE 推算已超过 {PROXY_MAX_DAYS} 天，等名单重建后再判"
                elif abs(pe / gate_pe - 1) < band:
                    entry["review"] = f"推算 PE 在门槛 {gate_pe:.1f} 倍 ±{band:.0%} 内，结论可能翻转"
        except Exception as exc:
            entry["error"] = f"取数失败：{exc}"
        fund = index["fund"]
        try:
            tracking_error = client.tracking_error(fund["fund_code"])
        except Exception:
            tracking_error = None
            failures += 1
        entry["product"] = live_product(fund, etf_rows.get(fund["code"]), tracking_error, today)
        entries.append(entry)
    return {
        "items": entries,
        "generated_at": universe.get("generated_at"),
        "tracking_failures": failures,
        "treasury_date": TREASURY_YIELD_DATE,
    }


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def execute(session_factory: sessionmaker[Session], run_id: int, client) -> None:
    def update(**fields) -> None:
        with session_factory() as session:
            row = session.get(IndexFundRun, run_id)
            if row is None:
                return
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = _now()
            session.commit()

    update(status="running", progress="开始取数")
    try:
        today = china_market_date()
        collected = collect(client, load_universe(), today, lambda text: update(progress=text))
        payload = build_payload(collected, treasury=Decimal(TREASURY_YIELD), today=today)
        payload["as_of"] = _now().isoformat()
        update(
            status="done",
            progress=None,
            error=None,
            payload_json=json.dumps(payload, ensure_ascii=False, default=_json_default),
        )
    except Exception as exc:
        logger.exception("指数基金刷新失败: %s", run_id)
        update(status="failed", progress=None, error=str(exc))
