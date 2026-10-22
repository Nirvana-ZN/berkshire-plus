"""Index-fund data: danjuan and csindex valuations, eastmoney ETFs, fund pages and returns."""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from discovery.constants import CHINA_MARKET_TIMEZONE

DANJUAN_API = "https://danjuanfunds.com/djapi"
CSINDEX_API = "https://www.csindex.com.cn/csindex-home"
FUND_PAGE = "https://fundf10.eastmoney.com"
FUND_CODES_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
PINGZHONG_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
KLINE_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
    "&klt=101&fqt={adjust}&lmt={days}&end=20500101&fields1=f1&fields2=f51,f53"
)
TENCENT_KLINE_URL = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={sym},day,,,{days},"
)
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
ETF_LIST_URL = (
    "https://push2delay.eastmoney.com/api/qt/clist/get?pn={page}&pz=100&po=1&np=1"
    "&fltt=2&fid=f6&fs=b:MK0021,b:MK0022,b:MK0023,b:MK0024"
    "&fields=f2,f6,f12,f13,f14,f20,f441"
)
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"}
# fundf10 throttles bursts (blank tables at zero gap); 0.3s held for 100 calls.
PAGE_GAP_SECONDS = 0.3
RETRY_PAUSE_SECONDS = 2.0
# csindex's WAF bans the IP for hours after ~150 calls at 1s spacing; callers
# stop at the first 403 instead of retrying into a longer ban.
CSINDEX_GAP_SECONDS = 3.0
RETURN_DAYS = 250

_CELL = re.compile(r"<th>([^<]+)</th>\s*<td[^>]*>(.*?)(?=<th>|</tr>)", re.S)
_TRACKING = re.compile(
    r"跟踪指数</th>.*?</tr>\s*<tr>\s*<td[^>]*>[^<]*</td>\s*<td[^>]*>([^<]*)</td>",
    re.S,
)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_CN_DATE = re.compile(r"(\d{4})年(\d{2})月(\d{2})日")
_NAV_TREND = re.compile(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", re.S)


class CsindexBlocked(RuntimeError):
    pass


def parse_profile(text: str) -> dict[str, str]:
    """Header -> value of the fund overview (jbgk) table."""
    return {
        key.strip(): html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
        for key, value in _CELL.findall(text)
    }


def parse_tracking_error(text: str) -> Decimal | None:
    match = _TRACKING.search(text)
    return percent(match.group(1)) if match else None


def percent(text: str | None) -> Decimal | None:
    """First number in a cell like '0.15%（每年）'; '---' means absent."""
    match = _NUMBER.search(text or "")
    return Decimal(match.group(0)) if match else None


def parse_cn_date(text: str | None) -> date | None:
    match = _CN_DATE.search(text or "")
    return date(*map(int, match.groups())) if match else None


def to_decimal(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def weekly_last(rows: list[tuple[str, Decimal | None]]) -> list[Decimal]:
    """Last positive value per ISO week from chronological (yyyymmdd, value) rows."""
    weeks: dict[tuple[int, int], Decimal] = {}
    for day, value in rows:
        if value is not None and value > 0:
            weeks[date(int(day[:4]), int(day[4:6]), int(day[6:8])).isocalendar()[:2]] = value
    return [weeks[key] for key in sorted(weeks)]


class FundClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.sleep = sleep
        self._client = None
        self._push2_alive = True

    def _http(self):
        if self._client is None:
            import httpx2

            self._client = httpx2.Client(
                timeout=self.timeout,
                headers=HEADERS,
                follow_redirects=True,
                trust_env=False,
            )
        return self._client

    def _get(self, url: str, **kwargs):
        for attempt in range(2):
            try:
                response = self._http().get(url, **kwargs)
                response.raise_for_status()
                return response
            except Exception:
                self._client = None
                if attempt == 1:
                    raise
                self.sleep(0.5)

    def _json(self, url: str) -> dict:
        return self._get(url).json()

    def _csindex(self, url: str) -> dict:
        self.sleep(CSINDEX_GAP_SECONDS)
        response = self._http().get(url, headers={"Referer": "https://www.csindex.com.cn/"})
        if response.status_code == 403:
            raise CsindexBlocked("中证官网限流（403），稍后再试")
        response.raise_for_status()
        return response.json()

    def _page(self, url: str, parse: Callable[[str], object]):
        """Paced fund-page fetch; one retry when the throttled page parses empty."""
        result = None
        for pause in (PAGE_GAP_SECONDS, RETRY_PAUSE_SECONDS):
            self.sleep(pause)
            result = parse(self._get(url).text)
            if result:
                return result
        return result

    def index_valuations(self) -> list[dict]:
        return self._json(f"{DANJUAN_API}/index_eva/dj")["data"]["items"]

    def valuation_history(self, index_code: str, metric: str) -> list[Decimal]:
        data = self._json(
            f"{DANJUAN_API}/index_eva/{metric}_history/{index_code}?day=all"
        )["data"]
        points = (to_decimal(row.get(metric)) for row in data[f"index_eva_{metric}_growths"])
        return [point for point in points if point is not None]

    def roe_history(self, index_code: str) -> list[Decimal]:
        """Weekly index ROE (fraction) from danjuan, chronological; starts late 2019."""
        return self.valuation_history(index_code, "roe")

    def index_closes(self, csindex_code: str, days: int = 400) -> dict[str, Decimal]:
        """Date -> price-index close (Tencent first for SH/SZ, then eastmoney).

        Some CSI "000xxx" indices live on market 2, not 1; market 0 is never tried
        for them because 0.000859 is a stock, not the index."""
        if csindex_code.startswith(("000", "399")) and days <= 800:
            sym = f"{'sz' if csindex_code.startswith('399') else 'sh'}{csindex_code}"
            try:
                data = self._json(TENCENT_KLINE_URL.format(sym=sym, days=days)).get("data")
                rows = data.get(sym, {}).get("day") if isinstance(data, dict) else None
                if rows:
                    return {row[0]: Decimal(row[2]) for row in rows}
            except Exception:
                pass
        if not self._push2_alive:
            return {}
        if csindex_code[0] in "H9":
            markets = ("2",)
        elif csindex_code.startswith(("399", "899")):
            markets = ("0",)
        else:
            markets = ("1", "2")
        for market in markets:
            try:
                data = self._json(
                    KLINE_URL.format(secid=f"{market}.{csindex_code}", adjust=0, days=days)
                ).get("data") or {}
            except Exception:
                self._push2_alive = False
                return {}
            closes = [line.split(",") for line in data.get("klines") or []]
            if closes:
                return {day: Decimal(close) for day, close in closes}
        return {}

    def passive_funds(self, index_code: str) -> list[dict]:
        data = self._json(
            f"{DANJUAN_API}/fundx/base/index/traces?symbol={index_code}"
        )["data"]
        return [
            fund
            for group in data.get("items") or []
            if group.get("fund_type") == "被动指数型"
            for fund in group.get("funds") or []
        ]

    def csindex_search(self, name: str) -> dict | None:
        """First csindex hit for a full index name; caller must verify the name."""
        data = self._csindex(
            f"{CSINDEX_API}/index-list/search-result-about-index"
            f"?searchInput={quote(name)}&pageNum=1&pageSize=5"
        ).get("data")
        return data[0] if data else None

    def csindex_pe(self, code: str, start: date, end: date) -> dict:
        """Full name, latest rolling PE with its date, weekly PE history over [start, end]."""
        rows = self._csindex(
            f"{CSINDEX_API}/perf/index-perf?indexCode={code}"
            f"&startDate={start:%Y%m%d}&endDate={end:%Y%m%d}"
        ).get("data") or []
        series = [(row["tradeDate"], to_decimal(row.get("peg"))) for row in rows]
        latest = next(((day, pe) for day, pe in reversed(series) if pe is not None), (None, None))
        return {
            "name": str(rows[-1].get("indexNameCnAll") or "") if rows else "",
            "pe_date": None if latest[0] is None else f"{latest[0][:4]}-{latest[0][4:6]}-{latest[0][6:]}",
            "pe_last": latest[1],
            "history": weekly_last(series),
        }

    def etf_list(self, codes: list[str] | None = None) -> list[dict]:
        rows: list[dict] = []
        page = 1
        try:
            while True:
                data = self._json(ETF_LIST_URL.format(page=page)).get("data") or {}
                diff = data.get("diff") or []
                for item in diff:
                    code = str(item.get("f12") or "")
                    rows.append(
                        {
                            "code": f"{code}.{'SH' if item.get('f13') == 1 else 'SZ'}",
                            "fund_code": code,
                            "name": str(item.get("f14") or "").strip(),
                            "price": to_decimal(item.get("f2")),
                            "turnover": to_decimal(item.get("f6")),
                            "market_cap": to_decimal(item.get("f20")),
                            "iopv": to_decimal(item.get("f441")),
                        }
                    )
                if not diff or len(rows) >= int(data.get("total") or 0):
                    return rows
                page += 1
        except Exception:
            self._push2_alive = False
            if not codes:
                raise
        rows = []
        for start in range(0, len(codes), 80):
            chunk = [f"{c[-2:].lower()}{c[:6]}" for c in codes[start : start + 80]]
            text = self._get(TENCENT_QUOTE_URL + ",".join(chunk)).content.decode("gbk", errors="replace")
            for line in text.splitlines():
                fields = line.split("~")
                if len(fields) > 78 and fields[3]:
                    suffix = "SH" if line.startswith("v_sh") else "SZ"
                    turnover = to_decimal(fields[37])
                    cap = to_decimal(fields[45])
                    rows.append(
                        {
                            "code": f"{fields[2]}.{suffix}",
                            "fund_code": fields[2],
                            "name": fields[1],
                            "price": to_decimal(fields[3]),
                            "turnover": None if turnover is None else turnover * 10000,
                            "market_cap": None if cap is None else cap * 100_000_000,
                            "iopv": to_decimal(fields[78]),
                        }
                    )
        return rows

    def fund_codes(self) -> list[tuple[str, str, str]]:
        """(code, name, eastmoney type) for every listed fund."""
        text = self._get(FUND_CODES_URL).content.decode("utf-8-sig")
        return [
            (row[0], row[2], row[3])
            for row in json.loads(text[text.index("[") : text.rindex("]") + 1])
        ]

    def fund_profile(self, fund_code: str) -> dict[str, str]:
        return self._page(f"{FUND_PAGE}/jbgk_{fund_code}.html", parse_profile)

    def tracking_error(self, fund_code: str) -> Decimal | None:
        return self._page(f"{FUND_PAGE}/tsdata_{fund_code}.html", parse_tracking_error)

    def daily_returns(self, fund: dict) -> dict[str, float]:
        """Date -> daily return (%) over the last ~year; dividends adjusted."""
        if fund["kind"] == "场内" and self._push2_alive:
            secid = f"{'1' if fund['code'].endswith('.SH') else '0'}.{fund['fund_code']}"
            try:
                data = self._json(
                    KLINE_URL.format(secid=secid, adjust=1, days=RETURN_DAYS + 1)
                ).get("data") or {}
                closes = [line.split(",") for line in data.get("klines") or []]
                if closes:
                    return {
                        day: (float(close) / float(prev) - 1) * 100
                        for (_, prev), (day, close) in zip(closes, closes[1:])
                        if float(prev) > 0
                    }
            except Exception:
                self._push2_alive = False
        match = _NAV_TREND.search(self._get(PINGZHONG_URL.format(code=fund["fund_code"])).text)
        points = json.loads(match.group(1))[-RETURN_DAYS:] if match else []
        return {
            datetime.fromtimestamp(p["x"] / 1000, CHINA_MARKET_TIMEZONE).date().isoformat(): float(p["equityReturn"])
            for p in points
            if p.get("equityReturn") is not None
        }
