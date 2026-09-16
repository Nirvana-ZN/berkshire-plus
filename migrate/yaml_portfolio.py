from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Cash, Instrument, Rule, Setting, Trade
from ledger.quotes import is_a_share_code

LOCAL_USER_ID = 1
MIGRATED_KEY = "yaml_migrated"
YAML_DIR_KEY = "portfolio_yaml_dir"
DIRECTION_MAP = {"买": "buy", "卖": "sell"}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class MigrateError(ValueError):
    """YAML 解析或迁入失败。"""


def is_migrated(session: Session) -> bool:
    row = session.get(Setting, MIGRATED_KEY)
    return row is not None and row.value == "true"


def stored_yaml_dir(session: Session) -> str | None:
    row = session.get(Setting, YAML_DIR_KEY)
    if row is None:
        return None
    value = (row.value or "").strip()
    return value or None


def migrate_yaml_portfolio(
    session: Session,
    yaml_dir: Path | None,
    *,
    user_id: int = LOCAL_USER_ID,
) -> bool:
    if yaml_dir is None:
        return False
    existing = session.scalar(
        select(func.count()).select_from(Trade).where(Trade.user_id == user_id)
    )
    if existing:
        return False
    if is_migrated(session):
        return False

    trades = _parse_trades(_load_yaml(yaml_dir / "trades.yaml", "trades.yaml"))
    cash_balance = _parse_cash(_load_yaml(yaml_dir / "cash.yaml", "cash.yaml"))
    rules = _parse_rules(_load_yaml(yaml_dir / "rules.yaml", "rules.yaml"))
    instruments = _parse_symbols(_load_yaml(yaml_dir / "symbols.yaml", "symbols.yaml"))

    try:
        for row in trades:
            session.add(Trade(user_id=user_id, **row))
        session.add(
            Cash(
                user_id=user_id,
                balance=cash_balance,
                currency="CNY",
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        for row in rules:
            session.add(Rule(user_id=user_id, **row))
        for row in instruments:
            session.add(Instrument(user_id=user_id, **row))
        session.merge(Setting(key=MIGRATED_KEY, value="true"))
        session.merge(Setting(key=YAML_DIR_KEY, value=str(yaml_dir)))
        session.commit()
    except MigrateError:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        raise MigrateError("迁入写入失败") from exc
    return True


def _load_yaml(path: Path, label: str) -> object:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise MigrateError(f"{label} 不是有效的 UTF-8 文本") from exc
    except OSError as exc:
        raise MigrateError(f"无法读取 {label}") from exc
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise MigrateError(f"{label} 解析失败") from exc


def _require_decimal(value: object, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MigrateError(f"{label}不是有效数字") from exc
    if not number.is_finite():
        raise MigrateError(f"{label}不是有效数字")
    return number


def _parse_trade_date(raw: object) -> str:
    if isinstance(raw, datetime):
        raw = raw.date()
    if isinstance(raw, date):
        return raw.isoformat()
    text = str(raw).strip()
    if not ISO_DATE.match(text):
        raise MigrateError(f"成交日期必须是 YYYY-MM-DD: {text}")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise MigrateError(f"成交日期无效: {text}") from exc
    return text


def _parse_trades(data: object) -> list[dict[str, object]]:
    if not isinstance(data, dict) or "成交" not in data:
        raise MigrateError("trades.yaml 缺少「成交」")
    items = data["成交"] or []
    if not isinstance(items, list):
        raise MigrateError("trades.yaml「成交」必须是列表")
    rows: list[dict[str, object]] = []
    accumulated: dict[str, Decimal] = {}
    for item in items:
        if not isinstance(item, dict):
            raise MigrateError("成交记录格式无效")
        try:
            raw_direction = str(item["方向"])
            direction = DIRECTION_MAP.get(raw_direction)
            if direction is None:
                raise MigrateError(f"未知成交方向: {raw_direction}")
            name = str(item["名称"]).strip()
            if not name:
                raise MigrateError("成交名称不能为空")
            shares = _require_decimal(item["股数"], "股数")
            price = _require_decimal(item["价格"], "价格")
            if shares <= 0:
                raise MigrateError("股数必须大于 0")
            if price <= 0:
                raise MigrateError("价格必须大于 0")
            trade_date = _parse_trade_date(item["日期"])
            if direction == "sell":
                held = accumulated.get(name, Decimal("0"))
                if shares > held:
                    raise MigrateError(f"卖出超过持仓: {name}")
                accumulated[name] = held - shares
            else:
                accumulated[name] = accumulated.get(name, Decimal("0")) + shares
            rows.append(
                {
                    "date": trade_date,
                    "direction": direction,
                    "name": name,
                    "shares": shares,
                    "price": price,
                    "note": str(item["说明"]) if item.get("说明") is not None else None,
                }
            )
        except KeyError as exc:
            raise MigrateError(f"成交记录缺少字段: {exc.args[0]}") from exc
    return rows


def _parse_cash(data: object) -> Decimal:
    if not isinstance(data, dict) or "余额" not in data:
        raise MigrateError("cash.yaml 缺少「余额」")
    return _require_decimal(data["余额"], "余额")


def _parse_rules(data: object) -> list[dict[str, object]]:
    if not isinstance(data, dict) or "规则" not in data:
        raise MigrateError("rules.yaml 缺少「规则」")
    items = data["规则"] or []
    if not isinstance(items, list):
        raise MigrateError("rules.yaml「规则」必须是列表")
    rows: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            raise MigrateError("规则记录格式无效")
        try:
            note = item.get("说明")
            price = _require_decimal(item["价格"], "规则价格")
            if price < 0:
                raise MigrateError("规则价格不能为负")
            rows.append(
                {
                    "name": str(item["名称"]),
                    "rule_type": str(item["类型"]),
                    "price": price,
                    "note": str(note) if note is not None else None,
                }
            )
        except KeyError as exc:
            raise MigrateError(f"规则记录缺少字段: {exc.args[0]}") from exc
    return rows


def _parse_symbols(data: object) -> list[dict[str, object]]:
    if not isinstance(data, dict) or "映射" not in data:
        raise MigrateError("symbols.yaml 缺少「映射」")
    mapping = data["映射"] or {}
    if not isinstance(mapping, dict):
        raise MigrateError("symbols.yaml「映射」必须是字典")
    rows: list[dict[str, object]] = []
    for name, code in mapping.items():
        if code is None:
            raise MigrateError(f"代码不能为空: {name}")
        code_text = str(code).strip()
        if not code_text:
            raise MigrateError(f"代码不能为空: {name}")
        if not is_a_share_code(code_text):
            raise MigrateError(f"不是有效的 A 股代码: {code_text}")
        rows.append({"code": code_text, "name": str(name)})
    return rows
