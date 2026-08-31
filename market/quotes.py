from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Protocol
import re

A_SHARE_CODE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
_TENCENT_LINE = re.compile(r"v_([a-z]{2}\d+)=\"([^\"]*)\"")
_SUFFIX_TO_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="


class QuoteClient(Protocol):
    def fetch_prices(self, codes: Sequence[str]) -> dict[str, Decimal]:
        """Return prices keyed by A-share code. Missing keys mean that code failed."""


class FakeQuoteClient:
    def __init__(
        self,
        prices: Mapping[str, Decimal | str | int | float] | None = None,
        *,
        failures: Sequence[str] | None = None,
        raise_on_fetch: Exception | None = None,
    ) -> None:
        self.prices = {code: Decimal(str(price)) for code, price in (prices or {}).items()}
        self.failures = set(failures or [])
        self.raise_on_fetch = raise_on_fetch
        self.calls: list[tuple[str, ...]] = []

    def fetch_prices(self, codes: Sequence[str]) -> dict[str, Decimal]:
        self.calls.append(tuple(codes))
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch
        result: dict[str, Decimal] = {}
        for code in codes:
            if code in self.failures:
                continue
            price = self.prices.get(code)
            if price is None:
                continue
            result[code] = price
        return result


def to_tencent_symbol(code: str) -> str:
    text = code.strip().upper()
    if not A_SHARE_CODE.match(text):
        raise ValueError(f"不是有效的 A 股代码: {code}")
    number, suffix = text.split(".")
    return f"{_SUFFIX_TO_PREFIX[suffix]}{number}"


def parse_tencent_body(text: str) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    for match in _TENCENT_LINE.finditer(text):
        symbol, payload = match.group(1), match.group(2)
        if not payload:
            continue
        fields = payload.split("~")
        if len(fields) < 4:
            continue
        try:
            price = Decimal(fields[3])
        except (InvalidOperation, ValueError):
            continue
        if price <= 0 or not price.is_finite():
            continue
        prices[symbol] = price
    return prices


def _decode_tencent(content: bytes) -> str:
    for encoding in ("gbk", "utf-8"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


TENCENT_SYMBOL_BATCH = 80


class TencentQuoteClient:
    def __init__(
        self,
        *,
        timeout: float = 10.0,
        base_url: str = TENCENT_QUOTE_URL,
    ) -> None:
        self.timeout = timeout
        self.base_url = base_url

    def fetch_prices(self, codes: Sequence[str]) -> dict[str, Decimal]:
        if not codes:
            return {}
        import httpx2

        reverse: dict[str, str] = {}
        symbols: list[str] = []
        for code in codes:
            try:
                symbol = to_tencent_symbol(code)
            except ValueError:
                continue
            symbols.append(symbol)
            reverse[symbol] = code
        if not symbols:
            return {}
        prices: dict[str, Decimal] = {}
        try:
            with httpx2.Client(timeout=self.timeout) as client:
                for start in range(0, len(symbols), TENCENT_SYMBOL_BATCH):
                    chunk = symbols[start : start + TENCENT_SYMBOL_BATCH]
                    url = self.base_url + ",".join(chunk)
                    try:
                        response = client.get(url)
                        response.raise_for_status()
                        parsed = parse_tencent_body(_decode_tencent(response.content))
                    except Exception:
                        continue
                    for symbol, price in parsed.items():
                        code = reverse.get(symbol)
                        if code is not None:
                            prices[code] = price
        except Exception:
            return prices
        return prices
