from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from threading import Event
from typing import Protocol
from urllib.parse import quote

from discovery.constants import china_market_date
from discovery.field_dictionary import FIELD_DICTIONARY
from market.cheap import (
    INDUSTRY_MAPPING_VERSION,
    CheapRow,
    delisting_name_state,
    is_st_name,
)
from market.types import (
    Constituent,
    DiscoveryCompanyData,
    DiscoveryCompanyError,
    DiscoveryPreRequest,
    DiscoveryRequestStopped,
    DiscoverySourceAsOf,
    FinancialRow,
    MarketError,
    SecurityState,
    TransientMarketError,
    ValuationPoint,
    validate_security_state,
)

BOARD_LIST_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
HS_A_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
CODETABLE_URL = "https://search-codetable.eastmoney.com/codetable/search"
FINANCIALS_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
ORGINFO_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
STOCK_URL = "https://push2delay.eastmoney.com/api/qt/stock/get"
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
CLIST_PAGE_RETRY_ROUNDS = 3
DIRECT_RETRY_ATTEMPTS = 3
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
    @property
    def request_count(self) -> int:
        """Total external requests made by this client instance."""

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

    def fetch_cheap_universe(
        self,
        *,
        before_request: DiscoveryPreRequest | None = None,
        as_of: date | None = None,
    ) -> list[CheapRow]:
        """沪深 A 股截面：现价 / PE / PB / 市值 / 行业。"""

    def fetch_valuation_history(self, code: str) -> list[ValuationPoint]:
        """Return one source-reported PE/PB point per month for five years."""

    def fetch_trading_dates(self, end: str, limit: int = 6) -> list[str]:
        """Return actual exchange dates ending no later than end."""

    def fetch_discovery_data(
        self,
        code: str,
        *,
        before_request: DiscoveryPreRequest | None = None,
        security_state: SecurityState | None = None,
        as_of: date | None = None,
    ) -> DiscoveryCompanyData:
        """Return one verified company bundle or raise DiscoveryCompanyError."""


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
        discovery_data: Mapping[str, DiscoveryCompanyData] | None = None,
        discovery_errors: Mapping[str, str] | None = None,
        discovery_request_counts: Mapping[str, int] | None = None,
        discovery_gate: Event | None = None,
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
        self.discovery_data = dict(discovery_data or {})
        self.discovery_errors = dict(discovery_errors or {})
        self.discovery_request_counts = dict(discovery_request_counts or {})
        self.discovery_gate = discovery_gate
        self.discovery_security_states: list[SecurityState | None] = []
        self.cheap_as_of: list[date] = []
        self._request_count = 0

    @property
    def request_count(self) -> int:
        return self._request_count

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

    def fetch_cheap_universe(
        self,
        *,
        before_request: DiscoveryPreRequest | None = None,
        as_of: date | None = None,
    ) -> list[CheapRow]:
        snapshot_date = as_of or china_market_date()
        self.cheap_as_of.append(snapshot_date)
        if before_request is not None:
            before_request()
        self._request_count += 1
        self.calls.append(("cheap",))
        if self.cheap_error is not None:
            raise MarketError(self.cheap_error)
        self._last_cheap_trading_dates = tuple(
            day
            for day in self.trading_dates
            if day <= snapshot_date.isoformat()
        )[-6:]
        return list(self.cheap_universe)

    def fetch_valuation_history(self, code: str) -> list[ValuationPoint]:
        self.calls.append(("valuation_history", code))
        return [row for row in self.valuation_history if row.code == code]

    def fetch_trading_dates(self, end: str, limit: int = 6) -> list[str]:
        self.calls.append(("trading_dates", end, str(limit)))
        return [day for day in self.trading_dates if day <= end][-limit:]

    def fetch_discovery_data(
        self,
        code: str,
        *,
        before_request: DiscoveryPreRequest | None = None,
        security_state: SecurityState | None = None,
        as_of: date | None = None,
    ) -> DiscoveryCompanyData:
        normalized = (code or "").strip().upper()
        if security_state is not None:
            try:
                validate_security_state(security_state, normalized)
            except ValueError as exc:
                raise DiscoveryCompanyError(
                    normalized, "security_state", str(exc)
                ) from exc
        self.discovery_security_states.append(security_state)
        request_count = self.discovery_request_counts.get(code)
        if code in self.discovery_errors:
            used = 0 if request_count is None else request_count
            for _ in range(used):
                if before_request is not None:
                    before_request()
            self._request_count += used
            self.calls.append(("discovery_data", code))
            if self.discovery_gate is not None:
                self.discovery_gate.wait(timeout=10)
            raise DiscoveryCompanyError(
                code,
                "fake",
                self.discovery_errors[code],
                request_count=used,
            )
        try:
            result = self.discovery_data[code]
        except KeyError as exc:
            used = 0 if request_count is None else request_count
            for _ in range(used):
                if before_request is not None:
                    before_request()
            self._request_count += used
            self.calls.append(("discovery_data", code))
            if self.discovery_gate is not None:
                self.discovery_gate.wait(timeout=10)
            raise DiscoveryCompanyError(
                code,
                "fake",
                "no configured discovery data",
                request_count=used,
            ) from exc
        used = result.request_count if request_count is None else request_count
        for _ in range(used):
            if before_request is not None:
                before_request()
        self._request_count += used
        self.calls.append(("discovery_data", code))
        if self.discovery_gate is not None:
            self.discovery_gate.wait(timeout=10)
        if security_state is not None:
            result = replace(
                result,
                security_state=security_state,
                source_as_of=replace(
                    result.source_as_of,
                    security=security_state.industry_as_of,
                ),
            )
        return result if used == result.request_count else replace(result, request_count=used)

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


def _report_kind(
    item: Mapping[str, object], *, strict: bool = False
) -> tuple[str, str] | None:
    year = str(item.get("REPORT_YEAR") or item.get("REPORT_DATE") or "")[:4]
    if len(year) != 4 or not year.isdigit():
        return None
    date_type = str(item.get("DATE_TYPE_CODE") or "")
    if date_type == "001":
        return year, "annual"
    if date_type == "002":
        return f"{year}H1", "interim"
    if date_type == "003":
        return f"{year}Q1", "quarterly"
    if date_type == "004":
        return f"{year}Q3", "quarterly"
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
    if strict and not report:
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


def _first_decimal(
    item: Mapping[str, object], *fields: str
) -> Decimal | None:
    for field in fields:
        value = _as_decimal(item.get(field))
        if value is not None:
            return value
    return None


def _dictionary_decimal(
    metric: str, item: Mapping[str, object]
) -> Decimal | None:
    spec = FIELD_DICTIONARY[metric]
    return (
        _as_decimal(item.get(spec.field))
        if spec.trusted and isinstance(spec.field, str)
        else None
    )


def _dictionary_financial_decimal(
    metric: str, item: Mapping[str, object]
) -> Decimal | None:
    field = FIELD_DICTIONARY[metric].financial_sector_substitute
    return _as_decimal(item.get(field)) if isinstance(field, str) else None


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


def _sum_known(*values: Decimal | None) -> Decimal | None:
    known = [value for value in values if value is not None]
    return sum(known, Decimal(0)) if known else None


def _validated_date(value: object) -> str | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _source_as_of(*items: Mapping[str, object]) -> str | None:
    dates = [
        parsed
        for item in items
        for field in ("UPDATE_DATE", "NOTICE_DATE")
        if (parsed := _validated_date(item.get(field))) is not None
    ]
    return max(dates) if dates else None


def _record_precedence(
    item: Mapping[str, object],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    return (
        _source_as_of(item) or "",
        tuple(sorted((str(key), str(value)) for key, value in item.items())),
    )


def _period_sort_key(kind: tuple[str, str]) -> tuple[int, int]:
    period, _period_type = kind
    rank = (
        1
        if period.endswith("Q1")
        else 2
        if period.endswith("H1")
        else 3
        if period.endswith("Q3")
        else 4
    )
    return int(period[:4]), rank


def _impairment_loss(item: Mapping[str, object]) -> Decimal | None:
    components: list[Decimal | None] = []
    fields = FIELD_DICTIONARY["impairment_loss"].field
    assert isinstance(fields, tuple)
    for direct, income in ((fields[0], fields[2]), (fields[1], fields[3])):
        loss = _as_decimal(item.get(direct))
        reported_income = _as_decimal(item.get(income))
        components.append(
            loss
            if loss is not None
            else -reported_income
            if reported_income is not None
            else None
        )
    return _sum_known(*components)


def _is_financial_industry(item: Mapping[str, object]) -> bool:
    return any(
        _as_decimal(item.get(field)) is not None
        for field in ("NEWCAPITALADER", "HXYJBCZL", "NONPERLOAN")
    )


def _financial_branch(
    industry: str | None, main_rows: Sequence[Mapping[str, object]]
) -> bool:
    from screen.funnel import _bank_or_insurance

    return _bank_or_insurance(industry or "") or any(
        _is_financial_industry(row) for row in main_rows
    )


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
        self._request_count = 0

    @property
    def request_count(self) -> int:
        return self._request_count

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

    def fetch_discovery_data(
        self,
        code: str,
        *,
        before_request: DiscoveryPreRequest | None = None,
        security_state: SecurityState | None = None,
        as_of: date | None = None,
    ) -> DiscoveryCompanyData:
        normalized = (code or "").strip().upper()
        started = self._request_count
        scoped_attempts = 0

        def scoped_before_request() -> None:
            nonlocal scoped_attempts
            assert before_request is not None
            before_request()
            scoped_attempts += 1

        request_gate = (
            scoped_before_request if before_request is not None else None
        )

        def request_count() -> int:
            return (
                scoped_attempts
                if request_gate is not None
                else self._request_count - started
            )

        try:
            _secid(normalized)
        except ValueError as exc:
            raise DiscoveryCompanyError(normalized, "validate", str(exc)) from exc
        if security_state is not None:
            try:
                validate_security_state(security_state, normalized)
                security = security_state
            except ValueError as exc:
                raise DiscoveryCompanyError(
                    normalized,
                    "security_state",
                    str(exc),
                    request_count=request_count(),
                ) from exc
        else:
            try:
                if request_gate is None:
                    security = (
                        self._security_state_for(normalized)
                        if as_of is None
                        else self._security_state_for(
                            normalized, as_of=as_of
                        )
                    )
                else:
                    security = (
                        self._security_state_for(
                            normalized, before_request=request_gate
                        )
                        if as_of is None
                        else self._security_state_for(
                            normalized,
                            before_request=request_gate,
                            as_of=as_of,
                        )
                    )
            except DiscoveryRequestStopped:
                raise
            except Exception as exc:
                raise DiscoveryCompanyError(
                    normalized,
                    "security_state",
                    str(exc),
                    request_count=request_count(),
                ) from exc
        try:
            financials = (
                self._discovery_financials_for(
                    normalized, industry=security.industry
                )
                if request_gate is None
                else self._discovery_financials_for(
                    normalized,
                    industry=security.industry,
                    before_request=request_gate,
                )
            )
        except DiscoveryRequestStopped:
            raise
        except Exception as exc:
            raise DiscoveryCompanyError(
                normalized,
                "financials",
                str(exc),
                request_count=request_count(),
            ) from exc
        try:
            valuations = (
                self.fetch_valuation_history(normalized)
                if request_gate is None
                else self.fetch_valuation_history(
                    normalized, before_request=request_gate
                )
            )
        except DiscoveryRequestStopped:
            raise
        except Exception as exc:
            raise DiscoveryCompanyError(
                normalized,
                "valuation",
                str(exc),
                request_count=request_count(),
            ) from exc
        if not financials:
            raise DiscoveryCompanyError(
                normalized,
                "financials",
                "no verified statement rows",
                request_count=request_count(),
            )
        financial_dates = [
            row.source_as_of for row in financials if row.source_as_of
        ]
        return DiscoveryCompanyData(
            code=normalized,
            security_state=security,
            financial_rows=tuple(financials),
            valuation_points=tuple(valuations),
            request_count=request_count(),
            source_as_of=DiscoverySourceAsOf(
                security=security.industry_as_of,
                valuation=max(
                    (row.trade_date for row in valuations), default=None
                ),
                financial=max(financial_dates, default=None),
            ),
        )

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

    def fetch_valuation_history(
        self,
        code: str,
        *,
        before_request: DiscoveryPreRequest | None = None,
    ) -> list[ValuationPoint]:
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
            payload = (
                self._get_json(url)
                if before_request is None
                else self._get_json(url, before_request=before_request)
            )
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

    def fetch_trading_dates(
        self,
        end: str,
        limit: int = 6,
        *,
        before_request: DiscoveryPreRequest | None = None,
        retry_transient: bool = False,
    ) -> list[str]:
        if limit <= 0:
            return []
        compact_end = end.replace("-", "")
        if len(compact_end) != 8 or not compact_end.isdigit():
            raise MarketError(f"不是有效日期: {end}")
        from market.sentiment import parse_kline

        try:
            request = (
                self._get_json_direct_retry
                if retry_transient
                else self._get_json_direct
            )
            parsed = parse_kline(
                request(_kline_url(limit, end=compact_end))
                if before_request is None
                else request(
                    _kline_url(limit, end=compact_end),
                    before_request=before_request,
                )
            )
        except DiscoveryRequestStopped:
            raise
        except TransientMarketError as exc:
            attempts = DIRECT_RETRY_ATTEMPTS if retry_transient else 1
            raise TransientMarketError(
                f"calendar fetch failed after {attempts} attempt"
                f"{'s' if attempts != 1 else ''}"
            ) from exc
        except MarketError as exc:
            raise MarketError(f"calendar response invalid: {exc}") from exc
        except Exception as exc:
            raise MarketError(
                f"calendar fetch failed: {type(exc).__name__}"
            ) from exc
        dates = parsed.get("dates")
        if not isinstance(dates, list) or not dates:
            raise MarketError("calendar response invalid: 交易日接口返回格式错误")
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

    def fetch_cheap_universe(
        self,
        *,
        before_request: DiscoveryPreRequest | None = None,
        as_of: date | None = None,
    ) -> list[CheapRow]:
        # ponytail: 东财截面约 50–80 页；历史分位另存每日快照
        snapshot_date = (as_of or china_market_date()).isoformat()
        try:
            diff = (
                self._clist_diff(
                    fs=HS_A_FS,
                    fields="f12,f13,f14,f2,f9,f23,f20,f26,f100,f292",
                    fid="f12",
                    max_pages=80,
                )
                if before_request is None
                else self._clist_diff(
                    fs=HS_A_FS,
                    fields="f12,f13,f14,f2,f9,f23,f20,f26,f100,f292",
                    fid="f12",
                    max_pages=80,
                    before_request=before_request,
                )
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
            exchange_dates = (
                self.fetch_trading_dates(
                    snapshot_date, retry_transient=True
                )
                if before_request is None
                else self.fetch_trading_dates(
                    snapshot_date,
                    before_request=before_request,
                    retry_transient=True,
                )
            )
        latest_exchange_date = exchange_dates[-1] if exchange_dates else None
        self._last_cheap_trading_dates = tuple(exchange_dates)
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
            # f292=6 is only a suspension candidate, so even a retained price
            # needs a stock-kline check. Other positive rows use the frozen
            # exchange date; uninterpreted statuses without price stay unknown.
            if market_status == 6 and latest_exchange_date is not None:
                probed_last_trade_date = (
                    self._last_trade_date(
                        code,
                        latest_exchange_date,
                        retry_transient=True,
                    )
                    if before_request is None
                    else self._last_trade_date(
                        code,
                        latest_exchange_date,
                        before_request=before_request,
                        retry_transient=True,
                    )
                )
                if (
                    probed_last_trade_date is not None
                    and probed_last_trade_date < latest_exchange_date
                ):
                    trading_status = "suspended"
                    last_trade_date = probed_last_trade_date
                elif probed_last_trade_date == latest_exchange_date:
                    trading_status = "trading"
                    last_trade_date = latest_exchange_date
                else:
                    trading_status = None
                    last_trade_date = None
            elif (
                price is not None
                and price > 0
                and latest_exchange_date is not None
            ):
                trading_status = "trading"
                last_trade_date = latest_exchange_date
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

    def _security_state_for(
        self,
        code: str,
        *,
        before_request: DiscoveryPreRequest | None = None,
        as_of: date | None = None,
    ) -> SecurityState:
        snapshot_date = as_of or china_market_date()
        fields = "f57,f58,f107,f127,f189,f292"
        url = f"{STOCK_URL}?secid={_secid(code)}&fields={fields}"
        payload = (
            self._get_json_direct(url)
            if before_request is None
            else self._get_json_direct(
                url, before_request=before_request
            )
        )
        item = payload.get("data")
        if not isinstance(item, dict) or str(item.get("f57") or "") != code[:6]:
            raise MarketError("个股状态接口返回格式错误")
        name = str(item.get("f58") or "").strip()
        industry = str(item.get("f127") or "").strip()
        listing_date = _compact_date(item.get("f189"))
        exchange = code.rsplit(".", 1)[1]
        expected_market = 1 if exchange == "SH" else 0
        if (
            not name
            or not industry
            or listing_date is None
            or _status_code(item.get("f107")) != expected_market
        ):
            raise MarketError("个股状态缺少名称、行业、上市日期或交易所不匹配")
        trading_dates = (
            self.fetch_trading_dates(snapshot_date.isoformat())
            if before_request is None
            else self.fetch_trading_dates(
                snapshot_date.isoformat(),
                before_request=before_request,
            )
        )
        if not trading_dates:
            raise MarketError("交易日接口没有返回日期")
        last_trade_date = (
            self._last_trade_date(code, trading_dates[-1])
            if before_request is None
            else self._last_trade_date(
                code,
                trading_dates[-1],
                before_request=before_request,
            )
        )
        if last_trade_date is None:
            raise MarketError("个股最近交易日不可用")
        trading_status = (
            "trading" if last_trade_date == trading_dates[-1] else "suspended"
        )
        return SecurityState(
            code=code,
            name=name,
            exchange=exchange,
            listing_date=listing_date,
            is_st=is_st_name(name),
            is_delisting=bool(delisting_name_state(code, name)),
            trading_status=trading_status,
            last_trade_date=last_trade_date,
            industry=industry,
            industry_version=INDUSTRY_MAPPING_VERSION,
            industry_as_of=snapshot_date.isoformat(),
            source="eastmoney:stock/get:f57,f58,f107,f127,f189,f292",
            market_status_code=_status_code(item.get("f292")),
        )

    def _last_trade_date(
        self,
        code: str,
        end: str,
        *,
        before_request: DiscoveryPreRequest | None = None,
        retry_transient: bool = False,
    ) -> str | None:
        from market.sentiment import parse_kline

        try:
            url = _kline_url(
                1,
                end=end.replace("-", ""),
                secid=_secid(code),
            )
            request = (
                self._get_json_direct_retry
                if retry_transient
                else self._get_json_direct
            )
            payload = (
                request(url)
                if before_request is None
                else request(url, before_request=before_request)
            )
            parsed = parse_kline(payload)
        except DiscoveryRequestStopped:
            raise
        except TransientMarketError as exc:
            if retry_transient:
                raise TransientMarketError(
                    f"suspended K-line probe failed for {code} "
                    f"after {DIRECT_RETRY_ATTEMPTS} attempts"
                ) from exc
            return None
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

    def _get_json_direct(
        self,
        url: str,
        *,
        before_request: DiscoveryPreRequest | None = None,
    ) -> dict:
        import httpx2

        if before_request is not None:
            before_request()
        self._request_count += 1
        try:
            with httpx2.Client(timeout=self.timeout, headers=_HTTP_HEADERS) as client:
                response = client.get(url)
        except DiscoveryRequestStopped:
            raise
        except httpx2.TransportError as exc:
            raise TransientMarketError(
                f"direct request failed: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            raise MarketError(
                f"direct request failed: {type(exc).__name__}"
            ) from exc

        status_code = int(response.status_code)
        if not 200 <= status_code < 300:
            try:
                response.raise_for_status()
            except Exception as exc:
                cause = exc
            else:
                cause = None
            # Retry exactly HTTP 408, 429, and the complete 500-599 range.
            error_type = (
                TransientMarketError
                if status_code in {408, 429} or 500 <= status_code <= 599
                else MarketError
            )
            raise error_type(f"direct request HTTP {status_code}") from cause
        try:
            payload = response.json()
        except (ValueError, httpx2.DecodingError) as exc:
            raise TransientMarketError(
                f"direct response JSON decode failed: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            raise MarketError(
                f"direct response parse failed: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict):
            raise MarketError("接口返回不是对象")
        return payload

    def _get_json_direct_retry(
        self,
        url: str,
        *,
        before_request: DiscoveryPreRequest | None = None,
    ) -> dict:
        last_error: TransientMarketError | None = None
        for _attempt in range(DIRECT_RETRY_ATTEMPTS):
            try:
                return (
                    self._get_json_direct(url)
                    if before_request is None
                    else self._get_json_direct(
                        url, before_request=before_request
                    )
                )
            except DiscoveryRequestStopped:
                raise
            except TransientMarketError as exc:
                last_error = exc
        raise TransientMarketError(
            f"direct request failed after {DIRECT_RETRY_ATTEMPTS} attempts: "
            f"{type(last_error.__cause__).__name__ if last_error else 'unknown'}"
        ) from last_error

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

    def _get_json(
        self,
        url: str,
        *,
        before_request: DiscoveryPreRequest | None = None,
    ) -> dict:
        import httpx2

        last_error: Exception | None = None
        for candidate in _clist_urls(url):
            if before_request is not None:
                before_request()
            self._request_count += 1
            try:
                with httpx2.Client(timeout=self.timeout, headers=_HTTP_HEADERS) as client:
                    response = client.get(candidate)
                    response.raise_for_status()
                    payload = response.json()
            except DiscoveryRequestStopped:
                raise
            except Exception as exc:
                last_error = exc
                continue
            if isinstance(payload, dict):
                return payload
            raise MarketError("接口返回不是对象")
        raise TransientMarketError(
            (
                f"clist transport failed: {type(last_error).__name__}"
                if last_error
                else "clist transport failed"
            )
        ) from last_error

    def _clist_diff(
        self,
        *,
        fs: str,
        fields: str,
        fid: str,
        encode_fs: bool = False,
        max_pages: int = 50,
        before_request: DiscoveryPreRequest | None = None,
    ) -> list[dict]:
        rows: list[dict] = []
        seen: set[str] = set()
        encoded = quote(fs, safe=":+!") if encode_fs else fs
        for page in range(1, max_pages + 1):
            url = (
                f"{BOARD_LIST_URL}?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid={fid}"
                f"&fs={encoded}&fields={fields}"
            )
            last_error: TransientMarketError | None = None
            for attempt in range(1, CLIST_PAGE_RETRY_ROUNDS + 1):
                try:
                    payload = (
                        self._get_json(url)
                        if before_request is None
                        else self._get_json(
                            url, before_request=before_request
                        )
                    )
                    break
                except DiscoveryRequestStopped:
                    raise
                except TransientMarketError as exc:
                    last_error = exc
            else:
                detail = " ".join(str(last_error).split())[:200]
                raise MarketError(
                    f"clist page {page} fetch failed at attempt "
                    f"{attempt}/{CLIST_PAGE_RETRY_ROUNDS}: {detail}"
                ) from last_error
            data = payload.get("data")
            if data is None:
                diff = []
            elif not isinstance(data, dict):
                raise MarketError(f"clist page {page} payload data is not an object")
            else:
                diff = data.get("diff")
                if diff is None:
                    diff = []
                elif not isinstance(diff, list):
                    raise MarketError(
                        f"clist page {page} payload diff is not a list"
                    )
            if not diff:
                break
            previous_count = len(rows)
            for item in diff:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("f12") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                rows.append(item)
            if len(rows) == previous_count:
                raise MarketError(
                    f"clist page {page} repeated payload made no progress"
                )
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
        return self._financial_rows_for(code, strict=False)

    def _discovery_financials_for(
        self,
        code: str,
        *,
        industry: str | None = None,
        before_request: DiscoveryPreRequest | None = None,
    ) -> list[FinancialRow]:
        return (
            self._financial_rows_for(
                code, strict=True, industry=industry
            )
            if before_request is None
            else self._financial_rows_for(
                code,
                strict=True,
                industry=industry,
                before_request=before_request,
            )
        )

    def _financial_rows_for(
        self,
        code: str,
        *,
        strict: bool,
        industry: str | None = None,
        before_request: DiscoveryPreRequest | None = None,
    ) -> list[FinancialRow]:
        reports = {
            "main": "RPT_F10_FINANCE_MAINFINADATA",
            "income": "RPT_DMSK_FN_INCOME",
            "balance": "RPT_DMSK_FN_BALANCE",
            "cashflow": "RPT_DMSK_FN_CASHFLOW",
            "income_detail": "RPT_F10_FINANCE_GINCOME",
            "balance_detail": "RPT_F10_FINANCE_GBALANCE",
            "cashflow_detail": "RPT_F10_FINANCE_GCASHFLOW",
        }
        fetched: dict[str, list[dict]] = {
            "main": (
                self._financial_report_rows(code, reports["main"])
                if before_request is None
                else self._financial_report_rows(
                    code,
                    reports["main"],
                    before_request=before_request,
                )
            )
        }
        financial_branch = strict and _financial_branch(
            industry, fetched["main"]
        )
        for name, report in reports.items():
            if name == "main":
                continue
            if not strict:
                fetched[name] = []
                continue
            if financial_branch and name in {
                "balance_detail",
                "cashflow_detail",
            }:
                fetched[name] = []
                continue
            fetched[name] = (
                self._financial_report_rows(code, report)
                if before_request is None
                else self._financial_report_rows(
                    code,
                    report,
                    before_request=before_request,
                )
            )

        mapped: dict[str, dict[tuple[str, str], dict]] = {}
        keys: set[tuple[str, str]] = set()
        for name, records in fetched.items():
            by_period: dict[tuple[str, str], dict] = {}
            for item in records:
                kind = _report_kind(item, strict=strict)
                if kind is None:
                    continue
                current = by_period.get(kind)
                if current is None or (
                    strict
                    and _record_precedence(item)
                    > _record_precedence(current)
                ):
                    by_period[kind] = item
                keys.add(kind)
            mapped[name] = by_period

        rows: list[FinancialRow] = []
        for period, period_type in sorted(
            keys, key=_period_sort_key, reverse=True
        ):
            empty: dict[str, object] = {}
            main = mapped["main"].get((period, period_type), empty)
            income = mapped["income"].get((period, period_type), empty)
            balance = mapped["balance"].get((period, period_type), empty)
            cashflow = mapped["cashflow"].get((period, period_type), empty)
            income_detail = mapped["income_detail"].get(
                (period, period_type), empty
            )
            balance_detail = mapped["balance_detail"].get(
                (period, period_type), empty
            )
            cashflow_detail = mapped["cashflow_detail"].get(
                (period, period_type), empty
            )

            def metric(
                name: str, item: Mapping[str, object]
            ) -> Decimal | None:
                value = _dictionary_decimal(name, item)
                return (
                    _dictionary_financial_decimal(name, main)
                    if financial_branch and value is None
                    else value
                )

            shares = (
                _first_decimal(
                    main, "TOTAL_SHARE", "TOTALSHARE", "TOTAL_SHARES"
                )
                if not strict
                else None
            )
            ebit = (
                _mul(_as_decimal(main.get("PER_EBIT")), shares)
                if not strict
                else None
            )
            if not strict and ebit is None:
                ebit = _as_decimal(main.get("EBIT"))
            net_income = metric("net_income", income)
            if not strict and net_income is None:
                net_income = _first_decimal(
                    main, "PARENTNETPROFIT", "NETPROFIT"
                )
            ocf = metric("ocf", cashflow)
            if not strict and ocf is None:
                ocf = _first_decimal(
                    main,
                    "NETCASH_OPERATE_PK",
                    "NETCASHFLOWOPERATE",
                    "OPERATECASHFLOW",
                )
            capex = metric("capex", cashflow)
            if not strict and capex is None:
                capex = _first_decimal(
                    main,
                    "PURCHASEFIXEDASSET",
                    "CONSTRUCTCASHPAY",
                    "CAPEX",
                )
            short_loan = metric("short_loan", balance_detail)
            long_loan = metric("long_loan", balance_detail)
            bond_payable = metric("bond_payable", balance_detail)
            noncurrent_liab_1year = metric(
                "noncurrent_liab_1year", balance_detail
            )
            # NONCURRENT_LIAB_1YEAR can include non-interest-bearing items and
            # therefore overstate debt. Keep every component visible.
            interest_debt = _sum_known(
                short_loan,
                long_loan,
                bond_payable,
                noncurrent_liab_1year,
            )
            rows.append(
                FinancialRow(
                    code=code,
                    period=period,
                    period_type=period_type,
                    roe=(
                        _first_decimal(
                            main, "ROEJQ", "ROE", "WEIGHTAVGROE"
                        )
                        if not strict
                        else None
                    ),
                    net_income=net_income,
                    ocf=ocf,
                    capex=capex,
                    ebit=ebit,
                    interest=(
                        _div(
                            ebit, _as_decimal(main.get("INTSTCOVRATE"))
                        )
                        if not strict
                        else None
                    ),
                    gross_margin=(
                        _first_decimal(
                            main, "XSMLL", "GROSSPROFITMARGIN"
                        )
                        if not strict
                        else None
                    ),
                    net_margin=(
                        _first_decimal(
                            main, "XSJLL", "NETPROFITMARGIN"
                        )
                        if not strict
                        else None
                    ),
                    shares=shares,
                    share_change_ma=None,
                    bps=(
                        _as_decimal(main.get("BPS"))
                        if not strict
                        else None
                    ),
                    npl=metric("npl", main),
                    eps=(
                        _first_decimal(main, "EPSJB", "EPSKCJB", "EPSXS")
                        if not strict
                        else None
                    ),
                    revenue=metric("revenue", income),
                    deducted_net_income=metric(
                        "deducted_net_income", income
                    ),
                    total_assets=metric("total_assets", balance),
                    net_assets=metric("net_assets", balance),
                    cash=metric("cash", balance),
                    short_loan=short_loan,
                    long_loan=long_loan,
                    bond_payable=bond_payable,
                    noncurrent_liab_1year=noncurrent_liab_1year,
                    interest_debt=interest_debt,
                    pretax_income=metric("pretax_income", income),
                    income_tax=metric("income_tax", income),
                    roic=(
                        None
                        if financial_branch
                        or _is_financial_industry(main)
                        else metric("roic", main)
                    ),
                    capital_adequacy=metric(
                        "capital_adequacy", main
                    ),
                    core_tier1_capital_adequacy=metric(
                        "core_tier1_capital_adequacy", main
                    ),
                    nonperforming_loan_balance=metric(
                        "nonperforming_loan_balance", main
                    ),
                    impairment_loss=_impairment_loss(income_detail),
                    government_grants=None,
                    asset_disposal_gain=metric(
                        "asset_disposal_gain", income_detail
                    ),
                    goodwill=metric("goodwill", balance_detail),
                    acquisition_cash_paid=metric(
                        "acquisition_cash_paid", cashflow_detail
                    ),
                    source_as_of=_source_as_of(
                        main,
                        income,
                        balance,
                        cashflow,
                        income_detail,
                        balance_detail,
                        cashflow_detail,
                    ),
                )
            )
        return rows

    def _financial_report_rows(
        self,
        code: str,
        report: str,
        *,
        before_request: DiscoveryPreRequest | None = None,
    ) -> list[dict]:
        filt = f'(SECUCODE="{code}")'
        url = (
            f"{FINANCIALS_URL}?reportName={report}"
            f"&columns=ALL&pageNumber=1&pageSize=50&sortColumns=REPORT_DATE"
            f"&sortTypes=-1&filter={quote(filt)}"
        )
        payload = (
            self._get_json(url)
            if before_request is None
            else self._get_json(url, before_request=before_request)
        )
        if payload.get("success") is False:
            raise MarketError(str(payload.get("message") or "财务接口整体不可用"))
        data = payload.get("result") or payload.get("data") or {}
        records = data.get("data") if isinstance(data, dict) else data
        if records is None:
            return []
        if not isinstance(records, list):
            raise MarketError("财务接口整体不可用")
        return [item for item in records if isinstance(item, dict)]

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
