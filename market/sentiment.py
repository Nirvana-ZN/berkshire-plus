from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from statistics import pstdev
from typing import Any

from sqlalchemy.orm import Session

from app.models import Setting

CACHE_KEY = "sentiment"
KLINE_CACHE_KEY = "sentiment_kline"
HIST_KEY = "sentiment_factor_hist"
TTL_SEC = 300
KLINE_STALE_SEC = 7 * 24 * 3600
HIST_MAX = 250
HIST_MIN = 60
KLINE_FACTORS = ("momentum", "volatility", "volume")
FEAR_MAX = 25
COOL_MAX = 41
WARM_MIN = 59
GREED_MIN = 75
SCOPE = "cni_a"
SCOPE_LABEL = "沪深A股（国证A指，不含北交所）"
METHOD = "v2.3"
CN_TZ = timezone(timedelta(hours=8))


def _q1(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _pct_rank(today: float, hist: list[float]) -> float:
    if not hist:
        return 50.0
    return sum(1 for item in hist if item <= today) / len(hist) * 100


def _pct_or_raw(today: float, hist: list[float]) -> float:
    if len(hist) < HIST_MIN:
        return today
    return _pct_rank(today, hist)


def _cn_now() -> datetime:
    return datetime.now(CN_TZ)


def _breadth_score(up: int, down: int) -> float | None:
    total = up + down
    if total <= 0:
        return None
    return up / total * 100


def _limits_score(zt: int | None, dt: int | None) -> float | None:
    if zt is None or dt is None:
        return None
    total = zt + dt
    if total <= 0:
        return 50.0
    return zt / total * 100


def _momentum_score(closes: list[float]) -> float | None:
    if len(closes) < 125:
        return None
    devs: list[float] = []
    for index in range(124, len(closes)):
        mean = sum(closes[index - 124 : index + 1]) / 125
        if mean == 0:
            continue
        devs.append(closes[index] / mean - 1)
    if not devs:
        return None
    return _pct_rank(devs[-1], devs)


def _volume_score(amounts: list[float]) -> float | None:
    if len(amounts) < 20:
        return None
    return _pct_rank(amounts[-1], amounts)


def _volatility_score(closes: list[float]) -> float | None:
    if len(closes) < 22:
        return None
    rets: list[float] = []
    for index in range(1, len(closes)):
        prev = closes[index - 1]
        if prev == 0:
            continue
        rets.append(closes[index] / prev - 1)
    if len(rets) < 20:
        return None
    vols = [pstdev(rets[index - 19 : index + 1]) for index in range(19, len(rets))]
    if not vols:
        return None
    return 100 - _pct_rank(vols[-1], vols)


def score_parts(
    *,
    up: int = 0,
    down: int = 0,
    pct: Decimal | None = None,
    zt: int | None = None,
    dt: int | None = None,
    closes: list[float] | None = None,
    amounts: list[float] | None = None,
    breadth_hist: list[float] | None = None,
    limits_hist: list[float] | None = None,
) -> dict[str, Any]:
    del pct
    # ponytail: 五因子等权；breadth 样本<60 用原始比例；limits 样本<60 不进合成（原始中性点在 85–90）；K 线 T-1、近一年分位；两融/北向/PutCall 有稳定源再加
    factors: dict[str, float] = {}
    breadth = _breadth_score(up, down)
    if breadth is not None:
        factors["breadth"] = _pct_or_raw(breadth, list(breadth_hist or []))
    limits_series = list(limits_hist or [])
    limits = _limits_score(zt, dt)
    if limits is not None and len(limits_series) >= HIST_MIN:
        factors["limits"] = _pct_or_raw(limits, limits_series)
    if closes:
        momentum = _momentum_score(closes)
        if momentum is not None:
            factors["momentum"] = momentum
        volatility = _volatility_score(closes)
        if volatility is not None:
            factors["volatility"] = volatility
    if amounts:
        volume = _volume_score(amounts)
        if volume is not None:
            factors["volume"] = volume
    if not factors:
        return {"score": None, "factors": {}}
    mix = sum(factors.values()) / len(factors)
    mix = min(100.0, max(0.0, mix))
    return {
        "score": int(Decimal(str(mix)).to_integral_value(rounding=ROUND_HALF_UP)),
        "factors": {key: _q1(value) for key, value in factors.items()},
    }


def score_from_breadth(
    *,
    up: int,
    down: int,
    pct: Decimal | None = None,
    zt: int | None = None,
    dt: int | None = None,
    closes: list[float] | None = None,
    amounts: list[float] | None = None,
    breadth_hist: list[float] | None = None,
    limits_hist: list[float] | None = None,
) -> int:
    return int(
        score_parts(
            up=up,
            down=down,
            pct=pct,
            zt=zt,
            dt=dt,
            closes=closes,
            amounts=amounts,
            breadth_hist=breadth_hist,
            limits_hist=limits_hist,
        )["score"]
        or 0
    )


def zone_of(score: int | None) -> tuple[str, str]:
    if score is None:
        return "unknown", "情绪不可用"
    if score <= FEAR_MAX:
        return "fear", "极端恐惧"
    if score >= GREED_MIN:
        return "greed", "极端贪婪"
    if score <= COOL_MAX:
        return "neutral", "中性偏冷"
    if score >= WARM_MIN:
        return "neutral", "中性偏热"
    return "neutral", "中性"


def parse_eastmoney_breadth(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("情绪接口返回不是对象")
    data = payload.get("data")
    row: dict | None = None
    if isinstance(data, dict):
        diff = data.get("diff")
        if isinstance(diff, list) and diff and isinstance(diff[0], dict):
            row = diff[0]
        elif isinstance(diff, dict):
            first = next(iter(diff.values()), None)
            row = first if isinstance(first, dict) else None
        elif "f3" in data or "f104" in data:
            row = data
    if row is None:
        raise ValueError("情绪接口缺少涨跌家数")
    up = _as_int(row.get("f104"))
    down = _as_int(row.get("f105"))
    if up is None or down is None:
        raise ValueError("情绪接口缺少涨跌家数")
    return {"up": up, "down": down, "flat": _as_int(row.get("f106")), "pct": _as_dec(row.get("f3"))}


def parse_kline(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data") or {}
    rows = data.get("klines") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}
    dates: list[str] = []
    closes: list[float] = []
    amounts: list[float] = []
    for item in rows:
        parts = str(item).split(",")
        if len(parts) < 7:
            continue
        day = parts[0].strip()[:10]
        close = _as_dec(parts[2])
        amount = _as_dec(parts[6])
        if not day or close is None or amount is None:
            continue
        dates.append(day)
        closes.append(float(close))
        amounts.append(float(amount))
    return {"dates": dates, "closes": closes, "amounts": amounts}


def drop_intraday_bar(
    raw: dict[str, Any],
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    dates = raw.get("dates")
    if not isinstance(dates, list) or not dates:
        return raw
    day = (today or _cn_now().date()).isoformat()
    clock = now or _cn_now()
    if str(dates[-1])[:10] != day or (clock.hour, clock.minute) >= (15, 0):
        return raw
    out = dict(raw)
    out["dates"] = dates[:-1]
    closes = raw.get("closes")
    amounts = raw.get("amounts")
    if isinstance(closes, list) and closes:
        out["closes"] = closes[:-1]
    if isinstance(amounts, list) and amounts:
        out["amounts"] = amounts[:-1]
    return out


def parse_limit_count(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    total = _as_int(data.get("tc") or data.get("total"))
    if total is not None:
        return total
    pool = data.get("pool")
    if isinstance(pool, list):
        return len(pool)
    return None


def _as_int(value: object) -> int | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _as_dec(value: object) -> Decimal | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _as_floats(value: object) -> list[float] | None:
    if not isinstance(value, list) or not value:
        return None
    out: list[float] = []
    for item in value:
        if isinstance(item, (int, float)) and item == item:
            out.append(float(item))
        elif isinstance(item, Decimal) and item.is_finite():
            out.append(float(item))
    return out or None


def _score_from_factors(factors: dict[str, object]) -> int | None:
    values = [float(value) for value in factors.values()]
    if not values:
        return None
    mix = min(100.0, max(0.0, sum(values) / len(values)))
    return int(Decimal(str(mix)).to_integral_value(rounding=ROUND_HALF_UP))


def apply_prior_kline(
    body: dict[str, Any],
    prior: dict[str, object] | None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    if not prior:
        return body
    fetched = prior.get("fetched_at")
    stamp = time.time() if now is None else now
    if not isinstance(fetched, (int, float)) or stamp - float(fetched) > KLINE_STALE_SEC:
        return body
    factors = dict(body.get("factors") or {})
    missing = [key for key in KLINE_FACTORS if key not in factors and key in prior]
    if not missing:
        return body
    for key in missing:
        factors[key] = prior[key]
    score = _score_from_factors(factors)
    zone, label = zone_of(score)
    filled = dict(body)
    filled["factors"] = factors
    filled["score"] = score
    filled["zone"] = zone
    filled["label"] = label
    return filled


def _kline_factors(factors: object) -> dict[str, object]:
    if not isinstance(factors, dict):
        return {}
    return {key: factors[key] for key in KLINE_FACTORS if key in factors}


def _load_hist(session: Session) -> list[dict[str, Any]]:
    row = session.get(Setting, HIST_KEY)
    if row is None or not row.value:
        return []
    try:
        body = json.loads(row.value)
    except json.JSONDecodeError:
        return []
    if not isinstance(body, list):
        return []
    return [item for item in body if isinstance(item, dict) and item.get("date")]


def _hist_day(raw: dict[str, Any]) -> str | None:
    dates = raw.get("dates")
    if isinstance(dates, list) and dates:
        return str(dates[-1])[:10]
    return None


def _upsert_hist(
    hist: list[dict[str, Any]],
    day: str,
    breadth: float | None,
    limits: float | None,
) -> list[dict[str, Any]]:
    # 按交易日一行；盘中多次打开覆盖当日行，基准是最后一次快照不是收盘
    incoming: dict[str, Any] = {"date": day}
    if breadth is not None:
        incoming["breadth"] = float(breadth)
    if limits is not None:
        incoming["limits"] = float(limits)
    for index, item in enumerate(hist):
        if str(item.get("date")) == day:
            row = {**item, **incoming}
            return (hist[:index] + [row] + hist[index + 1 :])[-HIST_MAX:]
    return (hist + [incoming])[-HIST_MAX:]


def _hist_series(hist: list[dict[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for row in hist:
        value = row.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value == value:
            out.append(float(value))
    return out


def payload_from_breadth(
    raw: dict[str, object],
    *,
    fetched_at: float,
    breadth_hist: list[float] | None = None,
    limits_hist: list[float] | None = None,
    today: date | None = None,
    clock: datetime | None = None,
) -> dict[str, Any]:
    raw = drop_intraday_bar(raw, today=today, now=clock)
    up = int(raw.get("up") or 0)
    down = int(raw.get("down") or 0)
    pct = raw.get("pct")
    pct_dec = pct if isinstance(pct, Decimal) else _as_dec(pct)
    parts = score_parts(
        up=up,
        down=down,
        pct=pct_dec,
        zt=_as_int(raw.get("zt")),
        dt=_as_int(raw.get("dt")),
        closes=_as_floats(raw.get("closes")),
        amounts=_as_floats(raw.get("amounts")),
        breadth_hist=breadth_hist,
        limits_hist=limits_hist,
    )
    score = parts["score"] if isinstance(parts["score"], int) else None
    zone, label = zone_of(score)
    pct_out = None
    if pct_dec is not None:
        pct_out = float(pct_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {
        "score": score,
        "zone": zone,
        "label": label,
        "scope": SCOPE,
        "scope_label": SCOPE_LABEL,
        "method": METHOD,
        "up": up,
        "down": down,
        "flat": _as_int(raw.get("flat")),
        "zt": _as_int(raw.get("zt")),
        "dt": _as_int(raw.get("dt")),
        "pct": pct_out,
        "factors": parts["factors"],
        "fetched_at": fetched_at,
    }


def _cached(session: Session, now: float) -> dict[str, Any] | None:
    row = session.get(Setting, CACHE_KEY)
    if row is None or not row.value:
        return None
    try:
        body = json.loads(row.value)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict) or body.get("method") != METHOD:
        return None
    fetched = body.get("fetched_at")
    if not isinstance(fetched, (int, float)) or now - fetched > TTL_SEC:
        return None
    return body


def _stored_kline(session: Session) -> dict[str, object]:
    row = session.get(Setting, KLINE_CACHE_KEY)
    if row is None or not row.value:
        return {}
    try:
        body = json.loads(row.value)
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def load_sentiment(session: Session, market: object | None) -> dict[str, Any]:
    now = time.time()
    cached = _cached(session, now)
    if cached is not None:
        filled = apply_prior_kline(cached, _stored_kline(session), now=now)
        if filled.get("factors") != cached.get("factors"):
            session.merge(Setting(key=CACHE_KEY, value=json.dumps(filled)))
        score = filled.get("score")
        filled["zone"], filled["label"] = zone_of(score if isinstance(score, int) else None)
        return filled
    raw = None
    fetch = getattr(market, "fetch_breadth", None)
    if callable(fetch):
        try:
            raw = fetch()
        except Exception:
            raw = None
    if not isinstance(raw, dict):
        zone, label = zone_of(None)
        return {"score": None, "zone": zone, "label": label, "fetched_at": now}
    hist = _load_hist(session)
    day = _hist_day(raw)
    if day:
        hist = _upsert_hist(
            hist,
            day,
            _breadth_score(int(raw.get("up") or 0), int(raw.get("down") or 0)),
            _limits_score(_as_int(raw.get("zt")), _as_int(raw.get("dt"))),
        )
        session.merge(Setting(key=HIST_KEY, value=json.dumps(hist)))
    fresh = payload_from_breadth(
        raw,
        fetched_at=now,
        breadth_hist=_hist_series(hist, "breadth"),
        limits_hist=_hist_series(hist, "limits"),
    )
    produced = _kline_factors(fresh.get("factors"))
    body = apply_prior_kline(fresh, _stored_kline(session), now=now)
    if produced:
        produced["fetched_at"] = now
        session.merge(Setting(key=KLINE_CACHE_KEY, value=json.dumps(produced)))
    session.merge(Setting(key=CACHE_KEY, value=json.dumps(body)))
    return body


def is_extreme_greed(session: Session, market: object | None) -> bool:
    return load_sentiment(session, market).get("zone") == "greed"
