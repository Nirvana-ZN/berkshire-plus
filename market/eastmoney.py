from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from threading import Event
from typing import Protocol
from urllib.parse import quote

from market.types import Constituent, FinancialRow, MarketError

BOARD_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
FINANCIALS_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
ORGINFO_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

_SCOPE_INDUSTRY = frozenset({"行业", "industry"})
_SCOPE_INDEX = frozenset({"指数", "index"})
_SCOPE_THEME = frozenset({"主题", "theme"})


class MarketClient(Protocol):
    def fetch_constituents(self, scope_type: str, scope_text: str) -> list[Constituent]:
        """Return the resolved constituent list, or raise MarketError."""

    def fetch_financials(self, codes: Sequence[str]) -> list[FinancialRow]:
        """Return annual rows. Raise MarketError if the endpoint is down."""

    def fetch_listed_years(self, codes: Sequence[str]) -> dict[str, int]:
        """Best-effort listing year by code. Partial misses are omitted."""


class FakeMarketClient:
    def __init__(
        self,
        constituents: Sequence[Constituent] | None = None,
        financials: Sequence[FinancialRow] | None = None,
        listed_years: Mapping[str, int] | None = None,
        *,
        constituent_error: str | None = None,
        financial_error: str | None = None,
        gate: Event | None = None,
    ) -> None:
        self.constituents = list(constituents or [])
        self.financials = list(financials or [])
        self.listed_years = dict(listed_years or {})
        self.constituent_error = constituent_error
        self.financial_error = financial_error
        self.gate = gate
        self.calls: list[tuple[str, ...]] = []

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


def _code_with_suffix(raw: str, market: object | None = None) -> str:
    digits = "".join(ch for ch in str(raw).strip() if ch.isdigit())
    if len(digits) != 6:
        return str(raw).strip()
    if str(market) == "1" or digits.startswith(("6", "9")):
        return f"{digits}.SH"
    if digits.startswith(("4", "8")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


class EastMoneyClient:
    def __init__(self, *, timeout: float = 15.0) -> None:
        self.timeout = timeout

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

    def _get_json(self, url: str) -> dict:
        import httpx2

        with httpx2.Client(timeout=self.timeout) as client:
            response = client.get(url)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise MarketError("接口返回不是对象")
        return payload

    def _constituents_from_board(self, name: str, *, board_type: int) -> list[Constituent]:
        url = (
            f"{BOARD_LIST_URL}?pn=1&pz=500&po=1&np=1&fltt=2&invt=2&fid=f3"
            f"&fs=m:90+t:{board_type}+f:!50&fields=f12,f14"
        )
        payload = self._get_json(url)
        diff = (payload.get("data") or {}).get("diff") or []
        matches = [
            item
            for item in diff
            if isinstance(item, dict) and str(item.get("f14") or "").strip() == name
        ]
        if len(matches) != 1:
            raise MarketError(f"解析不到明确列表: {name}")
        board = str(matches[0].get("f12") or "").strip()
        if not board:
            raise MarketError(f"解析不到明确列表: {name}")
        return self._clist_constituents(f"b:{board}+f:!50", industry=name)

    def _constituents_from_index(self, name: str) -> list[Constituent]:
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
        url = (
            f"{BOARD_LIST_URL}?pn=1&pz=500&po=1&np=1&fltt=2&invt=2&fid=f20"
            f"&fs={quote(fs, safe=':+!')}&fields=f12,f13,f14,f20,f9,f100,f136"
        )
        payload = self._get_json(url)
        diff = (payload.get("data") or {}).get("diff") or []
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
        for item in records:
            if not isinstance(item, dict):
                continue
            report = str(item.get("REPORT_TYPE") or item.get("REPORTTYPE") or "")
            if "半年" in report or "季" in report:
                continue
            if report and "年" not in report and report not in {"年报", "annual"}:
                continue
            period = str(item.get("REPORT_YEAR") or item.get("REPORT_DATE") or "")[:4]
            if len(period) != 4 or not period.isdigit():
                continue
            rows.append(
                FinancialRow(
                    code=code,
                    period=period,
                    period_type="annual",
                    roe=_as_decimal(item.get("ROEJQ") or item.get("ROE") or item.get("WEIGHTAVGROE")),
                    net_income=_as_decimal(
                        item.get("PARENTNETPROFIT") or item.get("NETPROFIT")
                    ),
                    ocf=_as_decimal(
                        item.get("NETCASHFLOWOPERATE") or item.get("OPERATECASHFLOW")
                    ),
                    capex=_as_decimal(
                        item.get("PURCHASEFIXEDASSET")
                        or item.get("CONSTRUCTCASHPAY")
                        or item.get("CAPEX")
                    ),
                    ebit=_as_decimal(item.get("EBIT") or item.get("OPERATEPROFIT")),
                    interest=_as_decimal(item.get("INTERESTEXPENSE") or item.get("FINANCEEXPENSE")),
                    gross_margin=_as_decimal(item.get("XSMLL") or item.get("GROSSPROFITMARGIN")),
                    net_margin=_as_decimal(item.get("XSJLL") or item.get("NETPROFITMARGIN")),
                    shares=_as_decimal(item.get("TOTALSHARE") or item.get("TOTAL_SHARES")),
                    share_change_ma=None,
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
        raw = first.get("LISTING_DATE") or first.get("FOUND_DATE") or ""
        text = str(raw)[:4]
        if text.isdigit():
            return int(text)
        return None
