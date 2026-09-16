from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from threading import Event
from typing import Protocol
from urllib.parse import quote

from market.cheap import (
    INDUSTRY_MAPPING_VERSION,
    CheapRow,
    delisting_name_state,
    is_st_name,
)
from market.types import Constituent, FinancialRow, MarketError, ValuationPoint

BOARD_LIST_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
HS_A_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
CODETABLE_URL = "https://search-codetable.eastmoney.com/codetable/search"
FINANCIALS_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
ORGINFO_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
VALUATION_PAGE_CAP = 20
BREADTH_URL = (
    "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
    "?fltt=2&secids=0.399317&fields=f3,f12,f104,f105,f106"
)
def _kline_url(
    limit: int, *, end: str = "20500101", secid: str = "0.399317"
) -> str:
    return (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&end={end}&lmt={limit}"
    )


KLINE_URL = _kline_url(375)
KLINE_URL_FALLBACK = _kline_url(250)
ZT_URL = (
    "https://push2ex.eastmoney.com/getTopicZTPool"
    "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
    "&Pageindex=0&pagesize=1&sort=fbt:asc&date={date}"
)
DT_URL = (
    "https://push2ex.eastmoney.com/getTopicDTPool"
    "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
    "&Pageindex=0&pagesize=1&sort=fundc:asc&date={date}"
)
CLIST_HOSTS = (
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
)
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}

_SCOPE_INDUSTRY = frozenset({"行业", "industry"})
_SCOPE_INDEX = frozenset({"指数", "index"})
_SCOPE_THEME = frozenset({"主题", "theme"})
_BOARD_ALIASES = {
    "农业": "农林牧渔",
    "农牧": "农林牧渔",
    "农林": "农林牧渔",
    "机械重工": "机械设备",
    "重工机械": "机械设备",
    "机械": "机械设备",
}
_BOARD_CODES = {
    "农业": "BK0433",
    "农林牧渔": "BK0433",
    "农牧": "BK0433",
    "农林": "BK0433",
    "机械重工": "BK1205",
    "重工机械": "BK1205",
    "机械": "BK1205",
    "机械设备": "BK1205",
    "工程机械": "BK0739",
}
_SUFFIXES = ("Ⅱ", "II", "2")


class MarketClient(Protocol):
    def fetch_constituents(self, scope_type: str, scope_text: str) -> list[Constituent]:
        """Return the resolved constituent list, or raise MarketError."""

    def fetch_financials(self, codes: Sequence[str]) -> list[FinancialRow]:
        """Return annual and interim rows. Raise MarketError if the endpoint is down."""

    def fetch_listed_years(self, codes: Sequence[str]) -> dict[str, int]:
        """Best-effort listing year by code. Partial misses are omitted."""

    def fetch_valuation(self, code: str) -> dict[str, object]:
        """Latest BPS / NPL / EPS. Partial misses omitted; do not raise."""

    def lookup_equity(self, query: str) -> Constituent:
        """Resolve one A-share by name or code, or raise MarketError."""

    def list_boards(self, scope_type: str, query: str = "") -> list[dict[str, str]]:
        """Return board or index names for the screen picker."""

    def fetch_cheap_universe(self) -> list[CheapRow]:
        """沪深 A 股截面：现价 / PE / PB / 市值 / 行业。"""

    def fetch_valuation_history(self, code: str) -> list[ValuationPoint]:
        """Return one source-reported PE/PB point per month for five years."""

    def fetch_trading_dates(self, end: str, limit: int = 6) -> list[str]:
        """Return actual exchange dates ending no later than end."""


class FakeMarketClient:
    def __init__(
        self,
        constituents: Sequence[Constituent] | None = None,
        financials: Sequence[FinancialRow] | None = None,
        listed_years: Mapping[str, int] | None = None,
        valuations: Mapping[str, Mapping[str, object]] | None = None,
        *,
        constituent_error: str | None = None,
        financial_error: str | None = None,
        gate: Event | None = None,
        boards: Sequence[Mapping[str, str]] | None = None,
        breadth: Mapping[str, object] | None = None,
        cheap_universe: Sequence[CheapRow] | None = None,
        cheap_error: str | None = None,
        valuation_history: Sequence[ValuationPoint] | None = None,
        trading_dates: Sequence[str] | None = None,
    ) -> None:
        self.constituents = list(constituents or [])
        self.financials = list(financials or [])
        self.listed_years = dict(listed_years or {})
        self.valuations = {code: dict(row) for code, row in (valuations or {}).items()}
        self.notices: list[dict[str, str]] = []
        self.constituent_error = constituent_error
        self.financial_error = financial_error
        self.gate = gate
        self.boards = [dict(row) for row in boards] if boards is not None else [
            {"name": "农林牧渔", "code": "BK0433"},
            {"name": "农业", "code": "BK0433"},
            {"name": "机械设备", "code": "BK1205"},
            {"name": "机械重工", "code": "BK1205"},
            {"name": "银行", "code": "BK0475"},
        ]
        self.calls: list[tuple[str, ...]] = []
        self.breadth = dict(breadth) if breadth is not None else {
            "up": 2000,
            "down": 2000,
            "pct": Decimal("0"),
            "zt": 40,
            "dt": 40,
        }
        self.cheap_universe = list(cheap_universe or [])
        self.cheap_error = cheap_error
        self.valuation_history = list(valuation_history or [])
        self.trading_dates = list(trading_dates or [])

    def fetch_constituents(self, scope_type: str, scope_text: str) -> list[Constituent]:
        self.calls.append(("constituents", scope_type, scope_text))
        if self.gate is not None:
            self.gate.wait(timeout=10)
        if self.constituent_error is not None:
            raise MarketError(self.constituent_error)
        return list(self.constituents)

    def fetch_financials(self, codes: Sequence[str]) -> list[FinancialRow]:
        self.calls.append(("financials", *codes))
        if self.financial_error is not None:
            raise MarketError(self.financial_error)
        wanted = set(codes)
        return [row for row in self.financials if row.code in wanted]

    def fetch_listed_years(self, codes: Sequence[str]) -> dict[str, int]:
        self.calls.append(("listed_years", *codes))
        return {code: year for code, year in self.listed_years.items() if code in codes}

    def fetch_valuation(self, code: str) -> dict[str, object]:
        self.calls.append(("valuation", code))
        return dict(self.valuations.get(code) or {})

    def fetch_notices(self, code: str) -> list[dict[str, str]]:
        self.calls.append(("notices", code))
        return [dict(row) for row in self.notices]

    def fetch_breadth(self) -> dict[str, object]:
        self.calls.append(("breadth",))
        return dict(self.breadth)

    def fetch_cheap_universe(self) -> list[CheapRow]:
        self.calls.append(("cheap",))
        if self.cheap_error is not None:
            raise MarketError(self.cheap_error)
        return list(self.cheap_universe)

    def fetch_valuation_history(self, code: str) -> list[ValuationPoint]:
        self.calls.append(("valuation_history", code))
        return [row for row in self.valuation_history if row.code == code]

    def fetch_trading_dates(self, end: str, limit: int = 6) -> list[str]:
        self.calls.append(("trading_dates", end, str(limit)))
        return [day for day in self.trading_dates if day <= end][-limit:]

    def list_boards(self, scope_type: str, query: str = "") -> list[dict[str, str]]:
        self.calls.append(("boards", scope_type, query))
        text = (query or "").strip()
        rows = list(self.boards)
        if text:
            rows = [row for row in rows if text in row["name"]]
        return rows

    def lookup_equity(self, query: str) -> Constituent:
        self.calls.append(("lookup", query))
        text = (query or "").strip()
        if not text:
            raise MarketError("个股为空，解析不到明确标的")
        needle = text.upper()
        matches: list[Constituent] = []
        for row in self.constituents:
            if row.name == text or row.code.upper() == needle:
                matches.append(row)
            elif text.isdigit() and len(text) == 6 and row.code.upper().startswith(text):
                matches.append(row)
        codes = {row.code for row in matches}
        if len(codes) == 1:
            return matches[0]
        raise MarketError(f"解析不到明确个股: {text}")


def _is_annual_report(item: Mapping[str, object]) -> bool:
    kind = _report_kind(item)
    return kind is not None and kind[1] == "annual"


def _report_kind(item: Mapping[str, object]) -> tuple[str, str] | None:
    year = str(item.get("REPORT_YEAR") or item.get("REPORT_DATE") or "")[:4]
    if len(year) != 4 or not year.isdigit():
        return None
    report = str(item.get("REPORT_TYPE") or item.get("REPORTTYPE") or "")
    if "半年" in report or "中报" in report:
        return f"{year}H1", "interim"
    if "一季" in report:
        return f"{year}Q1", "quarterly"
    if "三季" in report:
        return f"{year}Q3", "quarterly"
    if "季" in report:
        month = str(item.get("REPORT_DATE") or "")[5:7]
        if month == "03":
            return f"{year}Q1", "quarterly"
        if month == "06":
            return f"{year}H1", "interim"
        if month == "09":
            return f"{year}Q3", "quarterly"
        return None
    if report and "年" not in report and report not in {"年报", "annual"}:
        return None
    return year, "annual"


def _as_decimal(value: object) -> Decimal | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite():
        return None
    return number


def _positive_decimal(value: object) -> Decimal | None:
    number = _as_decimal(value)
    return number if number is not None and number > 0 else None


def _five_year_month_start(today: date) -> date:
    return date(today.year - 5, today.month, 1)


def _mul(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None or right is None:
        return None
    return left * right


def _div(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None or right is None or right == 0:
        return None
    return left / right


def _is_a_share_code(code: str) -> bool:
    text = (code or "").strip().upper()
    return (
        len(text) == 9
        and text[6] == "."
        and text[:6].isdigit()
        and text.endswith((".SH", ".SZ", ".BJ"))
    )


def _is_hs_code(code: str) -> bool:
    return _is_a_share_code(code) and code.upper().endswith((".SH", ".SZ"))


def _secid(code: str) -> str:
    text = code.strip().upper()
    if not _is_hs_code(text):
        raise ValueError(f"仅支持沪深 A 股代码: {code}")
    return f"{'1' if text.endswith('.SH') else '0'}.{text[:6]}"


def _compact_date(value: object) -> str | None:
    text = str(value or "")
    if len(text) != 8 or not text.isdigit():
        return None
    result = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        date.fromisoformat(result)
    except ValueError:
        return None
    return result


def _status_code(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _equity_from_item(item: Mapping[str, object]) -> tuple[str, str] | None:
    name = str(item.get("Name") or item.get("name") or "").strip()
    raw = str(item.get("Code") or item.get("code") or "").strip()
    quote_id = str(item.get("QuoteID") or item.get("quoteID") or "").strip()
    market = item.get("MktNum") or item.get("MarketType")
    if quote_id.count(".") == 1:
        prefix, digits = quote_id.split(".", 1)
        code = _code_with_suffix(digits, "1" if prefix == "1" else "0")
    else:
        code = _code_with_suffix(raw, market)
    if not name or not _is_a_share_code(code):
        return None
    return name, code


def pick_equity(query: str, items: Sequence[Mapping[str, object]]) -> tuple[str, str]:
    text = (query or "").strip()
    if not text:
        raise MarketError("个股为空，解析不到明确标的")
    parsed: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        got = _equity_from_item(item)
        if got:
            parsed.append(got)
    upper = text.upper()
    exact_name = [(name, code) for name, code in parsed if name == text]
    exact_code = [
        (name, code)
        for name, code in parsed
        if code.upper() == upper or (text.isdigit() and len(text) == 6 and code.startswith(text))
    ]
    if len({code for _, code in exact_name}) == 1:
        return exact_name[0]
    if len({code for _, code in exact_code}) == 1:
        return exact_code[0]
    if len({code for _, code in parsed}) == 1:
        return parsed[0]
    nearby = list(dict.fromkeys(name for name, _ in parsed))[:8]
    extra = f"（附近：{'、'.join(nearby)}）" if nearby else ""
    raise MarketError(f"解析不到明确个股: {text}{extra}")


def _code_with_suffix(raw: str, market: object | None = None) -> str:
    digits = "".join(ch for ch in str(raw).strip() if ch.isdigit())
    if len(digits) != 6:
        return str(raw).strip()
    if str(market) == "1" or digits.startswith(("6", "9")):
        return f"{digits}.SH"
    if digits.startswith(("4", "8")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


def _fold_board(text: str) -> str:
    value = (text or "").strip()
    for suffix in _SUFFIXES:
        if value.endswith(suffix):
            value = value[: -len(suffix)].strip()
            break
    return value


def _nearby_boards(name: str, titles: Sequence[str]) -> list[str]:
    query = name.strip()
    folded = _fold_board(query)
    prefix = query[:2] if len(query) >= 2 else query
    found: list[str] = []
    for title in titles:
        if title == query or _fold_board(title) == folded:
            found.append(title)
            continue
        if query and (query in title or title in query):
            found.append(title)
            continue
        if prefix and title.startswith(prefix):
            found.append(title)
            continue
        chunks = [query[i : i + 2] for i in range(len(query) - 1)] if len(query) >= 2 else []
        if any(chunk and chunk in title for chunk in chunks):
            found.append(title)
    return found


def pick_board(name: str, rows: Sequence[Mapping[str, object]]) -> dict:
    labeled: list[tuple[str, dict]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = str(item.get("f14") or "").strip()
        if title:
            labeled.append((title, item))
    titles = [title for title, _row in labeled]
    wanted = (name or "").strip()
    if not wanted:
        raise MarketError("范围文本为空，解析不到明确列表")

    exact = [row for title, row in labeled if title == wanted]
    if len(exact) == 1:
        return exact[0]

    alias = _BOARD_ALIASES.get(wanted)
    if alias:
        aliased = [row for title, row in labeled if title == alias or _fold_board(title) == _fold_board(alias)]
        if len(aliased) == 1:
            return aliased[0]

    folded = [row for title, row in labeled if _fold_board(title) == _fold_board(wanted)]
    if len(folded) == 1:
        return folded[0]

    nearby = _nearby_boards(wanted, titles)
    extra = f"。相近：{'、'.join(nearby[:8])}" if nearby else ""
    raise MarketError(f"解析不到明确列表: {wanted}{extra}")


def board_code(name: str) -> str | None:
    text = (name or "").strip()
    if text in _BOARD_CODES:
        return _BOARD_CODES[text]
    alias = _BOARD_ALIASES.get(text)
    if alias:
        return _BOARD_CODES.get(alias)
    return None


def _clist_urls(url: str) -> list[str]:
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)
    if "push2" not in parsed.netloc:
        return [url]
    urls: list[str] = []
    for host in CLIST_HOSTS:
        host_parsed = urlparse(host)
        urls.append(
            urlunparse(
                parsed._replace(scheme=host_parsed.scheme, netloc=host_parsed.netloc)
            )
        )
    if url not in urls:
        urls.append(url)
    return list(dict.fromkeys(urls))


def _index_secid(item: Mapping[str, object]) -> str:
    code = str(item.get("f12") or "").strip()
    market = item.get("f13")
    if not code:
        return ""
    if market is None or market == "":
        return code
    try:
        return f"{int(market)}.{code}"
    except (TypeError, ValueError):
        return f"{market}.{code}"


def _alias_rows(rows: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    by_name = {str(row["name"]): str(row["code"]) for row in rows}
    extra: list[dict[str, str]] = []
    for alias, official in _BOARD_ALIASES.items():
        code = by_name.get(official)
        if code and alias not in by_name:
            extra.append({"name": alias, "code": code})
    merged = [dict(row) for row in rows] + extra
    merged.sort(key=lambda row: row["name"])
    return merged


class EastMoneyClient:
    def __init__(self, *, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._board_lists: dict[int, list[dict[str, str]]] = {}

    def fetch_constituents(self, scope_type: str, scope_text: str) -> list[Constituent]:
        text = (scope_text or "").strip()
        if not text:
            raise MarketError("范围文本为空，解析不到明确列表")
        kind = (scope_type or "").strip()
        try:
            if kind in _SCOPE_INDUSTRY:
                return self._constituents_from_board(text, board_type=2)
            if kind in _SCOPE_THEME:
                return self._constituents_from_board(text, board_type=3)
            if kind in _SCOPE_INDEX:
                return self._constituents_from_index(text)
        except MarketError:
            raise
        except Exception as exc:
            raise MarketError(str(exc)) from exc
        raise MarketError(f"不支持的范围类型: {scope_type}")

    def list_boards(self, scope_type: str, query: str = "") -> list[dict[str, str]]:
        kind = (scope_type or "").strip()
        text = (query or "").strip()
        if kind in _SCOPE_INDEX:
            rows = self._list_index_catalog()
        elif kind in _SCOPE_INDUSTRY:
            rows = self._list_board_catalog(2)
        elif kind in _SCOPE_THEME:
            rows = self._list_board_catalog(3)
        else:
            raise MarketError(f"不支持的范围类型: {scope_type}")
        if not text:
            return rows
        return [row for row in rows if text in row["name"]]

    def fetch_financials(self, codes: Sequence[str]) -> list[FinancialRow]:
        if not codes:
            return []
        rows: list[FinancialRow] = []
        last_error: MarketError | None = None
        any_ok = False
        for code in codes:
            try:
                rows.extend(self._financials_for(code))
                any_ok = True
            except MarketError as exc:
                last_error = exc
            except Exception as exc:
                last_error = MarketError(str(exc))
        if not any_ok:
            raise last_error or MarketError("财务接口整体不可用")
        return rows

    def fetch_listed_years(self, codes: Sequence[str]) -> dict[str, int]:
        years: dict[str, int] = {}
        for code in codes:
            try:
                year = self._listed_year_for(code)
            except Exception:
                continue
            if year is not None:
                years[code] = year
        return years

    def fetch_valuation_history(self, code: str) -> list[ValuationPoint]:
        normalized = code.strip().upper()
        try:
            _secid(normalized)
        except ValueError as exc:
            raise MarketError(str(exc)) from exc
        start = _five_year_month_start(date.today())
        records: list[dict] = []
        seen_pages: set[tuple[tuple[str, str, str, str], ...]] = set()
        previous_oldest: date | None = None
        expected_pages: int | None = None
        pages_seen = False
        page = 1
        while True:
            filt = f'(SECURITY_CODE="{normalized[:6]}")'
            url = (
                f"{FINANCIALS_URL}?reportName=RPT_VALUEANALYSIS_DET"
                f"&columns=ALL&pageSize=500&pageNumber={page}"
                f"&sortColumns=TRADE_DATE&sortTypes=-1&filter={quote(filt)}"
            )
            payload = self._get_json(url)
            if payload.get("success") is not True:
                raise MarketError(str(payload.get("message") or "估值历史接口不可用"))
            result = payload.get("result")
            if not isinstance(result, dict) or "data" not in result:
                raise MarketError("估值历史接口返回格式错误")
            rows = result["data"]
            if not isinstance(rows, list):
                raise MarketError("估值历史接口返回格式错误")
            raw_pages = result.get("pages")
            if raw_pages is not None and (
                isinstance(raw_pages, bool)
                or not isinstance(raw_pages, int)
                or raw_pages < 0
            ):
                raise MarketError("估值历史接口分页信息无效")
            pages = raw_pages if isinstance(raw_pages, int) else None
            if not pages_seen:
                expected_pages = pages
                pages_seen = True
            elif pages != expected_pages:
                raise MarketError("估值历史接口分页信息前后不一致")
            if pages is not None and page > pages:
                raise MarketError("估值历史接口返回了超出总页数的中间页")
            if not rows:
                if page == 1 and pages in {None, 0}:
                    return []
                raise MarketError("估值历史接口中间页为空")

            parsed_rows: list[tuple[dict, str, date]] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise MarketError("估值历史接口包含无效记录")
                trade_date = str(row.get("TRADE_DATE") or "")[:10]
                try:
                    parsed_date = date.fromisoformat(trade_date)
                except ValueError as exc:
                    raise MarketError("估值历史接口包含无效交易日期") from exc
                parsed_rows.append((row, trade_date, parsed_date))
            signature = tuple(
                (
                    trade_date,
                    str(row.get("PE_TTM")),
                    str(row.get("PB_MRQ")),
                    str(row.get("CLOSE_PRICE")),
                )
                for row, trade_date, _parsed_date in parsed_rows
            )
            if signature in seen_pages:
                raise MarketError("估值历史接口重复分页，无进展")
            seen_pages.add(signature)
            oldest = min(parsed_date for _row, _trade_date, parsed_date in parsed_rows)
            if previous_oldest is not None and oldest >= previous_oldest:
                raise MarketError("估值历史接口分页无进展")
            previous_oldest = oldest
            records.extend(row for row, _trade_date, _parsed_date in parsed_rows)

            if oldest <= start:
                break
            if pages is not None and page >= pages:
                break
            if pages is None and len(rows) < 500:
                raise MarketError("估值历史接口提前结束，不足五年")
            if page >= VALUATION_PAGE_CAP:
                raise MarketError("估值历史接口超过分页安全上限")
            page += 1

        monthly: dict[str, tuple[str, dict]] = {}
        for row in records:
            trade_date = str(row.get("TRADE_DATE") or "")[:10]
            try:
                parsed_date = date.fromisoformat(trade_date)
            except ValueError:
                continue
            if parsed_date < start:
                continue
            month = trade_date[:7]
            previous = monthly.get(month)
            if previous is None or trade_date > previous[0]:
                monthly[month] = (trade_date, row)
        return [
            ValuationPoint(
                code=normalized,
                month=month,
                trade_date=trade_date,
                pe=_positive_decimal(row.get("PE_TTM")),
                pb=_positive_decimal(row.get("PB_MRQ")),
                price=_positive_decimal(row.get("CLOSE_PRICE")),
            )
            for month, (trade_date, row) in sorted(monthly.items(), reverse=True)
        ]

    def fetch_trading_dates(self, end: str, limit: int = 6) -> list[str]:
        if limit <= 0:
            return []
        compact_end = end.replace("-", "")
        if len(compact_end) != 8 or not compact_end.isdigit():
            raise MarketError(f"不是有效日期: {end}")
        from market.sentiment import parse_kline

        try:
            parsed = parse_kline(
                self._get_json_direct(_kline_url(limit, end=compact_end))
            )
        except MarketError:
            raise
        except Exception as exc:
            raise MarketError(str(exc)) from exc
        dates = parsed.get("dates")
        if not isinstance(dates, list) or not dates:
            raise MarketError("交易日接口返回格式错误")
        return [str(day)[:10] for day in dates][-limit:]

    def fetch_notices(self, code: str) -> list[dict[str, str]]:
        from market.cninfo import fetch_notices

        try:
            return fetch_notices(code, timeout=self.timeout)
        except Exception:
            return []

    def fetch_breadth(self) -> dict[str, object]:
        from market.sentiment import parse_eastmoney_breadth, parse_kline

        try:
            out = parse_eastmoney_breadth(self._get_json(BREADTH_URL))
        except MarketError:
            raise
        except Exception as exc:
            raise MarketError(str(exc)) from exc
        for url in (KLINE_URL, KLINE_URL_FALLBACK):
            try:
                parsed = parse_kline(self._get_json_direct(url))
            except Exception:
                continue
            if parsed.get("closes"):
                out.update(parsed)
                break
        out["zt"], out["dt"] = self._limit_counts()
        return out

    def fetch_cheap_universe(self) -> list[CheapRow]:
        # ponytail: 东财截面约 50–80 页；历史分位另存每日快照
        snapshot_date = date.today().isoformat()
        try:
            diff = self._clist_diff(
                fs=HS_A_FS,
                fields="f12,f13,f14,f2,f9,f23,f20,f26,f100,f292",
                fid="f12",
                max_pages=80,
            )
        except MarketError:
            raise
        except Exception as exc:
            raise MarketError(str(exc)) from exc
        exchange_dates: list[str] = []
        # ponytail: only f292=6 needs one stock-kline call (14/5,559 at probe);
        # batch this if the suspended set grows materially.
        if any(
            item.get("f292") is not None
            and (
                _positive_decimal(item.get("f2")) is not None
                or _status_code(item.get("f292")) == 6
            )
            for item in diff
        ):
            exchange_dates = self.fetch_trading_dates(snapshot_date)
        latest_exchange_date = exchange_dates[-1] if exchange_dates else None
        rows: list[CheapRow] = []
        for item in diff:
            if not isinstance(item, dict):
                continue
            code = _code_with_suffix(item.get("f12"), item.get("f13"))
            name = str(item.get("f14") or "").strip()
            if not name or not _is_a_share_code(code):
                continue
            if code.endswith(".BJ"):
                continue
            price = _as_decimal(item.get("f2"))
            market_status = _status_code(item.get("f292"))
            # f292=6 only selects suspension candidates. A priced row plus the
            # frozen exchange date verifies active trading; a lagging stock kline
            # verifies suspension. Other raw status values remain uninterpreted.
            if (
                price is not None
                and price > 0
                and latest_exchange_date is not None
            ):
                trading_status = "trading"
                last_trade_date = latest_exchange_date
            elif market_status == 6 and latest_exchange_date is not None:
                probed_last_trade_date = (
                    self._last_trade_date(code, latest_exchange_date)
                )
                if (
                    probed_last_trade_date is not None
                    and probed_last_trade_date < latest_exchange_date
                ):
                    trading_status = "suspended"
                    last_trade_date = probed_last_trade_date
                else:
                    trading_status = None
                    last_trade_date = None
            else:
                trading_status = None
                last_trade_date = None
            rows.append(
                CheapRow(
                    code=code,
                    name=name,
                    price=price,
                    pe=_as_decimal(item.get("f9")),
                    pb=_as_decimal(item.get("f23")),
                    mcap=_as_decimal(item.get("f20")),
                    industry=str(item.get("f100") or "").strip(),
                    exchange=code.rsplit(".", 1)[1],
                    listing_date=_compact_date(item.get("f26")),
                    is_st=is_st_name(name),
                    is_delisting=delisting_name_state(code, name),
                    trading_status=trading_status,
                    last_trade_date=last_trade_date,
                    # Our mapping revision and retrieval snapshot, not an East
                    # Money taxonomy edition or classification publication date.
                    industry_version=INDUSTRY_MAPPING_VERSION,
                    industry_as_of=snapshot_date,
                    security_source="eastmoney:push2delay",
                    market_status_code=market_status,
                )
            )
        if not rows:
            raise MarketError("低估值榜解析不到沪深A股名单")
        return rows

    def _last_trade_date(self, code: str, end: str) -> str | None:
        from market.sentiment import parse_kline

        try:
            parsed = parse_kline(
                self._get_json_direct(
                    _kline_url(
                        1,
                        end=end.replace("-", ""),
                        secid=_secid(code),
                    )
                )
            )
        except Exception:
            return None
        dates = parsed.get("dates")
        if not isinstance(dates, list) or not dates:
            return None
        return str(dates[-1])[:10]

    def _limit_counts(self) -> tuple[int | None, int | None]:
        from datetime import datetime, timedelta

        from market.sentiment import CN_TZ, parse_limit_count

        day = datetime.now(CN_TZ).date()
        for _ in range(7):
            stamp = day.strftime("%Y%m%d")
            try:
                zt = parse_limit_count(self._get_json_direct(ZT_URL.format(date=stamp)))
                dt = parse_limit_count(self._get_json_direct(DT_URL.format(date=stamp)))
            except Exception:
                zt = dt = None
            if zt is not None or dt is not None:
                return zt, dt
            day -= timedelta(days=1)
        return None, None

    def _get_json_direct(self, url: str) -> dict:
        import httpx2

        with httpx2.Client(timeout=self.timeout, headers=_HTTP_HEADERS) as client:
            response = client.get(url)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise MarketError("接口返回不是对象")
        return payload

    def fetch_valuation(self, code: str) -> dict[str, object]:
        # BPS/不良用最新一期；EPS 只用年报，避免季报把市盈率抬高一倍
        if not code:
            return {}
        try:
            records = self._mainfina_rows(code, page_size=8)
        except Exception:
            return {}
        latest = next((row for row in records if isinstance(row, dict)), None)
        annual = next((row for row in records if isinstance(row, dict) and _is_annual_report(row)), None)
        if not latest:
            return {}
        out: dict[str, object] = {}
        for key, raw in (
            ("bps", latest.get("BPS")),
            ("npl_pct", latest.get("NONPERLOAN")),
        ):
            number = _as_decimal(raw)
            if number is not None:
                out[key] = number
        period = str(latest.get("REPORT_YEAR") or latest.get("REPORT_DATE") or "")[:4]
        if period.isdigit():
            out["period"] = period
        if annual is not None:
            eps = _as_decimal(
                annual.get("EPSJB") or annual.get("EPSKCJB") or annual.get("EPSXS")
            )
            if eps is not None:
                out["eps"] = eps
            eps_period = str(annual.get("REPORT_YEAR") or annual.get("REPORT_DATE") or "")[:4]
            if eps_period.isdigit():
                out["eps_period"] = eps_period
        return out

    def _mainfina_rows(self, code: str, *, page_size: int) -> list[dict]:
        filt = f'(SECUCODE="{code}")'
        url = (
            f"{FINANCIALS_URL}?reportName=RPT_F10_FINANCE_MAINFINADATA"
            f"&columns=ALL&pageNumber=1&pageSize={page_size}&sortColumns=REPORT_DATE"
            f"&sortTypes=-1&filter={quote(filt)}"
        )
        payload = self._get_json(url)
        if payload.get("success") is False:
            raise MarketError(str(payload.get("message") or "财务接口整体不可用"))
        data = payload.get("result") or payload.get("data") or {}
        records = data.get("data") if isinstance(data, dict) else data
        if not isinstance(records, list):
            return []
        return [row for row in records if isinstance(row, dict)]

    def lookup_equity(self, query: str) -> Constituent:
        text = (query or "").strip()
        if not text:
            raise MarketError("个股为空，解析不到明确标的")
        url = f"{CODETABLE_URL}?keyword={quote(text)}&pageIndex=1&pageSize=10"
        payload = self._get_json(url)
        rows = payload.get("result") or []
        items: list[dict[str, object]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                items.append(
                    {
                        "Name": row.get("shortName") or row.get("Name"),
                        "Code": row.get("code") or row.get("Code"),
                        "MktNum": row.get("market"),
                    }
                )
        name, code = pick_equity(text, items)
        return Constituent(code=code, name=name, mcap=None, pe=None, industry="", peg=None)

    def _get_json(self, url: str) -> dict:
        import httpx2

        last_error: Exception | None = None
        for candidate in _clist_urls(url):
            try:
                with httpx2.Client(timeout=self.timeout, headers=_HTTP_HEADERS) as client:
                    response = client.get(candidate)
                    response.raise_for_status()
                    payload = response.json()
            except Exception as exc:
                last_error = exc
                continue
            if isinstance(payload, dict):
                return payload
            last_error = MarketError("接口返回不是对象")
        raise MarketError(str(last_error) if last_error else "接口返回不是对象")

    def _clist_diff(
        self,
        *,
        fs: str,
        fields: str,
        fid: str,
        encode_fs: bool = False,
        max_pages: int = 50,
    ) -> list[dict]:
        rows: list[dict] = []
        seen: set[str] = set()
        encoded = quote(fs, safe=":+!") if encode_fs else fs
        for page in range(1, max_pages + 1):
            url = (
                f"{BOARD_LIST_URL}?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid={fid}"
                f"&fs={encoded}&fields={fields}"
            )
            payload = self._get_json(url)
            diff = (payload.get("data") or {}).get("diff") or []
            if not isinstance(diff, list) or not diff:
                break
            for item in diff:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("f12") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                rows.append(item)
            if len(diff) < 100:
                break
        return rows

    def _list_board_catalog(self, board_type: int) -> list[dict[str, str]]:
        cached = self._board_lists.get(board_type)
        if cached is not None:
            return cached
        diff = self._clist_diff(
            fs=f"m:90+t:{board_type}+f:!50",
            fields="f12,f14",
            fid="f3",
        )
        rows: list[dict[str, str]] = []
        for item in diff:
            name = str(item.get("f14") or "").strip()
            code = str(item.get("f12") or "").strip()
            if name and code:
                rows.append({"name": name, "code": code})
        rows = _alias_rows(rows)
        self._board_lists[board_type] = rows
        return rows

    def _list_index_catalog(self) -> list[dict[str, str]]:
        cached = self._board_lists.get(-1)
        if cached is not None:
            return cached
        diff = self._clist_diff(
            fs="m:1+s:3,m:0+s:3",
            fields="f12,f13,f14",
            fid="f3",
        )
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in diff:
            name = str(item.get("f14") or "").strip()
            secid = _index_secid(item)
            if not name or not secid or name in seen:
                continue
            seen.add(name)
            rows.append({"name": name, "code": secid})
        rows.sort(key=lambda row: row["name"])
        self._board_lists[-1] = rows
        return rows

    def _constituents_from_board(self, name: str, *, board_type: int) -> list[Constituent]:
        mapped = board_code(name)
        if mapped:
            label = _BOARD_ALIASES.get(name, name)
            return self._clist_constituents(f"b:{mapped}+f:!50", industry=label)
        diff = self._clist_diff(
            fs=f"m:90+t:{board_type}+f:!50",
            fields="f12,f14",
            fid="f3",
        )
        if not diff:
            raise MarketError(f"解析不到明确列表: {name}")
        chosen = pick_board(name, diff)
        board = str(chosen.get("f12") or "").strip()
        if not board:
            raise MarketError(f"解析不到明确列表: {name}")
        label = str(chosen.get("f14") or "").strip() or name
        return self._clist_constituents(f"b:{board}+f:!50", industry=label)

    def _constituents_from_index(self, name: str) -> list[Constituent]:
        for row in self._list_index_catalog():
            if row["name"] == name and row["code"]:
                return self._clist_constituents(f"b:{row['code']}", industry=name)
        url = f"{SUGGEST_URL}?input={quote(name)}&type=14"
        payload = self._get_json(url)
        quotes = payload.get("QuotationCodeTable") or payload.get("data") or {}
        items = quotes.get("Data") if isinstance(quotes, dict) else quotes
        if not isinstance(items, list):
            items = []
        exact = [
            item
            for item in items
            if isinstance(item, dict)
            and str(item.get("Name") or item.get("name") or "").strip() == name
        ]
        if len(exact) != 1:
            raise MarketError(f"解析不到明确列表: {name}")
        code = str(exact[0].get("Code") or exact[0].get("code") or "").strip()
        quote_id = str(exact[0].get("QuoteID") or exact[0].get("quoteID") or "").strip()
        secid = quote_id if "." in quote_id else (f"1.{code}" if code else "")
        if not secid:
            raise MarketError(f"解析不到明确列表: {name}")
        return self._clist_constituents(f"b:{secid}", industry=name)

    def _clist_constituents(self, fs: str, *, industry: str) -> list[Constituent]:
        diff = self._clist_diff(
            fs=fs,
            fields="f12,f13,f14,f20,f9,f100,f136",
            fid="f20",
            encode_fs=True,
        )
        if not diff:
            raise MarketError(f"解析不到明确列表: {industry}")
        result: list[Constituent] = []
        for item in diff:
            if not isinstance(item, dict):
                continue
            code = _code_with_suffix(item.get("f12"), item.get("f13"))
            name = str(item.get("f14") or "").strip()
            if not name:
                continue
            industry_name = str(item.get("f100") or "").strip() or industry
            result.append(
                Constituent(
                    code=code,
                    name=name,
                    mcap=_as_decimal(item.get("f20")),
                    pe=_as_decimal(item.get("f9")),
                    industry=industry_name,
                    peg=_as_decimal(item.get("f136")),
                )
            )
        if not result:
            raise MarketError(f"解析不到明确列表: {industry}")
        return result

    def _financials_for(self, code: str) -> list[FinancialRow]:
        filt = f'(SECUCODE="{code}")'
        url = (
            f"{FINANCIALS_URL}?reportName=RPT_F10_FINANCE_MAINFINADATA"
            f"&columns=ALL&pageNumber=1&pageSize=50&sortColumns=REPORT_DATE"
            f"&sortTypes=-1&filter={quote(filt)}"
        )
        payload = self._get_json(url)
        if payload.get("success") is False:
            raise MarketError(str(payload.get("message") or "财务接口整体不可用"))
        data = payload.get("result") or payload.get("data") or {}
        records = data.get("data") if isinstance(data, dict) else data
        if records is None:
            return []
        if not isinstance(records, list):
            raise MarketError("财务接口整体不可用")
        rows: list[FinancialRow] = []
        seen: set[tuple[str, str]] = set()
        for item in records:
            if not isinstance(item, dict):
                continue
            kind = _report_kind(item)
            if kind is None:
                continue
            period, period_type = kind
            key = (period, period_type)
            if key in seen:
                continue
            seen.add(key)
            ocf = _as_decimal(
                item.get("NETCASH_OPERATE_PK")
                or item.get("NETCASHFLOWOPERATE")
                or item.get("OPERATECASHFLOW")
            )
            shares = _as_decimal(
                item.get("TOTAL_SHARE")
                or item.get("TOTALSHARE")
                or item.get("TOTAL_SHARES")
            )
            # 主要指标表只给每股EBIT和利息保障倍数，反推回绝对额喂利息覆盖门槛
            ebit = _mul(_as_decimal(item.get("PER_EBIT")), shares) or _as_decimal(
                item.get("EBIT")
            )
            interest = _div(ebit, _as_decimal(item.get("INTSTCOVRATE")))
            # ponytail: 主要指标表没有资本开支，自由现金流门槛只能留缺口；
            # 东财 FCFF_BACK/FCFF_FORWARD 口径互相打架(美的 +14.6亿 vs -339亿)不能用，
            # 要补就单独拉现金流量表
            capex = _as_decimal(
                item.get("PURCHASEFIXEDASSET")
                or item.get("CONSTRUCTCASHPAY")
                or item.get("CAPEX")
            )
            rows.append(
                FinancialRow(
                    code=code,
                    period=period,
                    period_type=period_type,
                    roe=_as_decimal(item.get("ROEJQ") or item.get("ROE") or item.get("WEIGHTAVGROE")),
                    net_income=_as_decimal(
                        item.get("PARENTNETPROFIT") or item.get("NETPROFIT")
                    ),
                    ocf=ocf,
                    capex=capex,
                    ebit=ebit,
                    interest=interest,
                    gross_margin=_as_decimal(item.get("XSMLL") or item.get("GROSSPROFITMARGIN")),
                    net_margin=_as_decimal(item.get("XSJLL") or item.get("NETPROFITMARGIN")),
                    shares=shares,
                    share_change_ma=None,
                    bps=_as_decimal(item.get("BPS")),
                    npl=_as_decimal(item.get("NONPERLOAN")),
                    eps=_as_decimal(
                        item.get("EPSJB") or item.get("EPSKCJB") or item.get("EPSXS")
                    ),
                )
            )
        return rows

    def _listed_year_for(self, code: str) -> int | None:
        filt = f'(SECUCODE="{code}")'
        url = (
            f"{ORGINFO_URL}?reportName=RPT_F10_BASIC_ORGINFO"
            f"&columns=ALL&pageNumber=1&pageSize=1&filter={quote(filt)}"
        )
        payload = self._get_json(url)
        data = payload.get("result") or payload.get("data") or {}
        records = data.get("data") if isinstance(data, dict) else data
        if not isinstance(records, list) or not records:
            return None
        first = records[0]
        if not isinstance(first, dict):
            return None
        raw = first.get("LISTING_DATE") or ""
        text = str(raw)[:4]
        if text.isdigit():
            return int(text)
        return None
