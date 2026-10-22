"""Normalized, sourced market assertions and conservative session execution.

These checks establish a deterministic accounting contract, not source authenticity.
T16 must verify retained source/coverage evidence before calling the pure engine.
"""

from calendar import monthrange
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json

from discovery.constants import CHINA_MARKET_TIMEZONE
from tracking.contracts import canonical_json


SHANGHAI = CHINA_MARKET_TIMEZONE
ACTION_TYPES = frozenset(('split', 'dividend_ex', 'dividend_paid', 'delisting', 'rights'))
EVENT_TYPES = ACTION_TYPES | {'bar', 'benchmark', 'calendar_day', 'coverage'}
MARKET_POLICY = {
    'version': 'paper-market-v1', 'timezone': 'Asia/Shanghai',
    'supported_market_dates_from': '1992-01-01', 'timezone_implementation': 'post_1991_fixed_UTC_plus_08',
    'calendar': 'explicit_open_and_closed_days_complete_bridge',
    'future_calendar': 'observed_by_cutoff_only', 'cutoff': 'as_of_local_day_end_exclusive',
    'price_basis': 'unadjusted_factor_one', 'open': '09:30:00',
    'actions': 'pre_open_effective_time_then_explicit_tie_sequence',
    'date_normalization': 'pre_open_accounting_v1',
    'coverage': 'explicit_bars_and_actions_assertions',
}


def decimal_text(value: object, *, positive=False) -> Decimal:
    try:
        number = Decimal(value) if type(value) is str else None
        if number is None or not number.is_finite() or number < 0 or (positive and number == 0):
            raise ValueError
        return number
    except (InvalidOperation, ValueError):
        raise ValueError('market number must be finite nonnegative decimal text') from None


def utc_time(value: object) -> datetime:
    try:
        stamp = datetime.fromisoformat(value) if type(value) is str else None
        if stamp is None or stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
            raise ValueError
        return stamp.astimezone(timezone.utc)
    except ValueError:
        raise ValueError('market time must be an exact aware UTC timestamp') from None


def local_day(row: Mapping[str, object]) -> date:
    return supported_day(utc_time(row['effective_at']).astimezone(SHANGHAI).date())


def supported_day(day: date) -> date:
    if day < date(1992, 1, 1):
        raise ValueError('unsupported historical timezone: paper-market-v1 requires dates from 1992-01-01')
    return day


def day_value(value: object) -> date:
    if type(value) is not str:
        raise ValueError('market date must be ISO date text')
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError('market date must be ISO date text')
    return supported_day(result)


def add_months(day: date, months: int) -> date:
    year, month0 = divmod(day.year * 12 + day.month - 1 + months, 12)
    return date(year, month0 + 1, min(day.day, monthrange(year, month0 + 1)[1]))


def first_session_after(day: date, calendar: Mapping[date, bool]) -> date | None:
    candidate = day + timedelta(days=1)
    while candidate in calendar:
        if calendar[candidate]:
            return candidate
        candidate += timedelta(days=1)
    return None


def executable(bar: Mapping[str, object], side: str, purchase_date: date | None = None) -> bool:
    if side not in ('buy', 'sell'):
        raise ValueError('invalid execution side')
    return (not bar['suspended'] and decimal_text(bar['volume']) > 0
            and not bar['open_limit_up' if side == 'buy' else 'open_limit_down']
            and (side == 'buy' or (purchase_date is not None and local_day(bar) > purchase_date)))


def _validate(row: Mapping[str, object]) -> dict[str, object]:
    item = json.loads(canonical_json(row))
    for key in ('event_id', 'economic_event_id', 'code', 'type', 'source_id'):
        if type(item.get(key)) is not str or not item[key].strip():
            raise ValueError(f'market event requires {key}')
    kind = item['type']
    if kind not in EVENT_TYPES:
        raise ValueError('unsupported market event type')
    effective = utc_time(item.get('effective_at'))
    supported_day(effective.astimezone(SHANGHAI).date())
    item['effective_at'] = effective.isoformat()
    item['observed_at'] = utc_time(item.get('observed_at')).isoformat()
    if item.get('supersedes_id') is not None and type(item['supersedes_id']) is not str:
        raise ValueError('invalid supersedes identity')
    if 'withdrawn' in item and type(item['withdrawn']) is not bool:
        raise ValueError('withdrawn must be boolean')
    if kind in ACTION_TYPES:
        if kind != 'dividend_paid' and effective.astimezone(SHANGHAI).time() > time(9, 30):
            raise ValueError('corporate action must use a pre-open accounting boundary')
        precision = item.get('effective_time_precision', 'exact')
        if precision not in ('exact', 'date'):
            raise ValueError('invalid effective time precision')
        if precision == 'date' and (item.get('normalization_policy') != 'pre_open_accounting_v1'
                or day_value(item.get('effective_date')) != local_day(item)):
            raise ValueError('date-only action requires explicit pre-open normalization metadata')
        if 'action_sequence' in item:
            sequence = decimal_text(item['action_sequence'])
            if sequence != sequence.to_integral_value():
                raise ValueError('action sequence must be integer text')
    if kind == 'bar':
        for key in ('open', 'close'):
            decimal_text(item.get(key), positive=True)
        decimal_text(item.get('volume'))
        for key in ('suspended', 'open_limit_up', 'open_limit_down'):
            if type(item.get(key)) is not bool:
                raise ValueError('bar trade status must be explicit boolean')
        if item.get('price_basis') != 'unadjusted' or decimal_text(item.get('adjustment_factor')) != 1:
            raise ValueError('price basis must be unadjusted with factor one')
    elif kind == 'split':
        decimal_text(item.get('factor'), positive=True)
    elif kind == 'dividend_ex':
        decimal_text(item.get('cash_per_share'))
        if day_value(item.get('pay_date')) < local_day(item):
            raise ValueError('dividend payment precedes ex date')
    elif kind == 'dividend_paid':
        if type(item.get('ex_event_id')) is not str or not item['ex_event_id']:
            raise ValueError('dividend payment requires original ex event_id')
    elif kind == 'delisting':
        if item.get('settlement_status') not in ('unresolved', 'settled'):
            raise ValueError('invalid delisting settlement status')
        if item['settlement_status'] == 'settled':
            decimal_text(item.get('cash_per_share'))
        elif item.get('cash_per_share') is not None:
            raise ValueError('unresolved delisting has no known cash settlement')
    elif kind == 'calendar_day':
        if item['code'] != 'MARKET' or day_value(item.get('date')) != local_day(item):
            raise ValueError('invalid market calendar day')
        if type(item.get('is_session')) is not bool:
            raise ValueError('calendar session assertion must be boolean')
    elif kind == 'coverage':
        if day_value(item.get('start_date')) > day_value(item.get('end_date')):
            raise ValueError('invalid coverage interval')
        for key in ('bars_complete', 'actions_complete'):
            if type(item.get(key)) is not bool:
                raise ValueError('coverage requires explicit bars/actions assertions')
    elif kind == 'benchmark':
        if (type(item.get('index_id')) is not str or item['index_id'] != item['code']
                or type(item.get('series_revision')) is not str or not item['series_revision']
                or item.get('return_convention') != 'total_return'
                or item.get('valuation_point') not in ('session_open', 'session_close')):
            raise ValueError('benchmark requires sourced total-return identity, revision and valuation point')
        decimal_text(item.get('total_return_level'), positive=True)
    return item


def select_events(events: Sequence[Mapping[str, object]], as_of: date) -> list[dict[str, object]]:
    """Select explicit economic-version chains at Shanghai end-of-day cutoff.

    A correction replaces the economic action, never adds a second split/dividend.
    Future calendar declarations may be used only when already observed.
    """
    if type(as_of) is not date:
        raise ValueError('as_of must be a date')
    supported_day(as_of)
    cutoff = datetime.combine(as_of + timedelta(days=1), time(), SHANGHAI).astimezone(timezone.utc)
    by_id = {}
    for raw in events:
        item = _validate(raw)
        identity = item['event_id']
        if identity in by_id and item != by_id[identity]:
            raise ValueError('conflicting market event_id')
        by_id[identity] = item
    groups = defaultdict(list)
    for item in by_id.values():
        previous = item.get('supersedes_id')
        if previous is not None:
            parent = by_id.get(previous)
            if (parent is None or previous == item['event_id']
                    or any(item[key] != parent[key] for key in ('economic_event_id', 'code', 'type'))
                    or utc_time(item['observed_at']) < utc_time(parent['observed_at'])):
                raise ValueError('invalid market supersedes chain')
        if utc_time(item['observed_at']) < cutoff and (
                item['type'] == 'calendar_day' or utc_time(item['effective_at']) < cutoff):
            groups[item['economic_event_id']].append(item)
    selected = []
    for versions in groups.values():
        identities = {item['event_id'] for item in versions}
        parents = [item['supersedes_id'] for item in versions if item.get('supersedes_id') in identities]
        heads = identities - set(parents)
        if len(heads) != 1 or len(parents) != len(set(parents)):
            raise ValueError('conflicting economic event versions require a single supersedes chain')
        head = by_id[heads.pop()]
        visited = set()
        current = head
        while current['event_id'] in identities and current['event_id'] not in visited:
            visited.add(current['event_id'])
            current = by_id.get(current.get('supersedes_id'), {})
            if not current:
                break
        if visited != identities:
            raise ValueError('unlinked economic event versions')
        selected.append(head)  # Retain withdrawal provenance in the calculation digest.
    slots, action_slots = {}, defaultdict(list)
    for item in selected:
        if item.get('withdrawn', False):
            continue
        kind, day = item['type'], local_day(item)
        if kind in ('bar', 'calendar_day', 'benchmark'):
            slot = (kind, item['code'], day, item.get('valuation_point'))
            if slot in slots:
                raise ValueError('conflicting market observation slot')
            slots[slot] = item
        if kind in ACTION_TYPES:
            action_slots[(item['code'], item['effective_at'])].append(item)
    for actions in action_slots.values():
        if len(actions) > 1:
            orders = [item.get('action_sequence') for item in actions]
            if None in orders or len({decimal_text(order) for order in orders}) != len(orders):
                raise ValueError('same-time actions require unique explicit sequence')
    return sorted(selected, key=lambda item: (utc_time(item['effective_at']), item['code'],
                  decimal_text(item.get('action_sequence', '0')), item['event_id']))
