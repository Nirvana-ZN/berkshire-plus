"""Independent, replayable 3/6/12-month paper ledgers; no database or HTTP I/O.

Capital is normalized to 1 CNY. All arithmetic uses precision 34. Frozen equal
weights remain display/audit facts; sorted codes receive their original weight,
and the last code receives the exact remaining cash to absorb rounding only.
Gross and sensitivity paths repeat the same execution rules with separate cash.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from decimal import Context, Decimal, localcontext

from tracking.contracts import canonical_json, digest, protocol, validated_config
from tracking.market import (ACTION_TYPES, MARKET_POLICY, SHANGHAI, add_months, day_value,
                             decimal_text, executable, first_session_after, local_day,
                             select_events, supported_day, utc_time)
from tracking.metrics import equity_return, max_drawdown


ZERO = Decimal('0')
ONE = Decimal('1')
ENGINE_VERSION = 'paper-ledger-v1'


def _text(value: Decimal) -> str:
    if value == 0:
        return '0'
    result = format(value, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def dividend_receivable(shares: Decimal, cash_per_share: Decimal) -> Decimal:
    for value in (shares, cash_per_share):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError('dividend amounts must be finite nonnegative Decimal values')
    with localcontext(Context(prec=34)):
        return shares * cash_per_share


@dataclass
class _Position:
    weight: str
    cash: Decimal
    shares: Decimal = ZERO
    price: Decimal = ZERO
    receivables: dict[str, Decimal] = field(default_factory=dict)
    purchase_date: date | None = None
    exit_date: date | None = None
    entry_status: str = 'pending'
    delisted: bool = False
    unresolved: bool = False

    @property
    def receivable(self):
        return sum(self.receivables.values(), ZERO)

    @property
    def equity(self):
        return self.cash + self.shares * self.price + self.receivable


def _manifest(manifest):
    canonical_json(manifest)
    if manifest.get('manifest_hash') != digest({key: value for key, value in manifest.items()
                                               if key != 'manifest_hash'}):
        raise ValueError('invalid frozen manifest hash')
    frozen = manifest['protocol']
    config = validated_config(frozen['config'])
    if frozen != protocol(config) or manifest.get('protocol_hash') != digest(frozen):
        raise ValueError('unsupported or corrupt frozen tracking protocol')
    members = manifest['members']
    count = manifest['original_member_count']
    if type(count) is not int or count < 0 or count != len(members):
        raise ValueError('invalid original member count')
    if len({item['code'] for item in members}) != count:
        raise ValueError('duplicate cohort member')
    expected_weight = ONE / count if count else ZERO
    for item in members:
        if type(item['code']) is not str or not item['code']:
            raise ValueError('invalid member code')
        if decimal_text(item['weight']) != expected_weight:
            raise ValueError('frozen member weight does not match original equal-member count')
    publication = manifest.get('published_run_at')
    if publication is None:
        published = None
    elif manifest.get('publication_time_status') != 'recorded':
        raise ValueError('publication timestamp lacks immutable recorded receipt')
    else:
        published = supported_day(utc_time(publication).astimezone(SHANGHAI).date())
    return config, sorted(members, key=lambda item: item['code']), published


class _Market:
    def __init__(self, selected, original, codes, published, as_of):
        self.calendar = {day_value(row['date']): row['is_session']
                         for row in selected if row['type'] == 'calendar_day'}
        self.bars = {(row['code'], local_day(row)): row for row in selected if row['type'] == 'bar'}
        self.actions = defaultdict(list)
        self.gaps = []
        self.benchmarks = [row for row in selected if row['type'] == 'benchmark']
        originals = {row['event_id']: row for row in original}
        self.ex_links = {key: row['economic_event_id'] for key, row in originals.items()
                         if row['type'] == 'dividend_ex'}
        ex_events = {row['economic_event_id']: row for row in selected if row['type'] == 'dividend_ex'}
        paid = set()
        for row in selected:
            if row['type'] == 'dividend_paid':
                ex_id = self.ex_links.get(row['ex_event_id'])
                ex = ex_events.get(ex_id)
                if (ex is None or ex['code'] != row['code'] or ex_id in paid
                        or local_day(row) < day_value(ex['pay_date'])):
                    raise ValueError('dividend payment has missing, duplicate or invalid ex-event link')
                paid.add(ex_id)
            if row['type'] in ACTION_TYPES and row['code'] in codes:
                self.actions[local_day(row)].append(row)
        self.coverage = [row for row in selected if row['type'] == 'coverage']
        if published is None:
            return
        for day in _days(published, as_of):
            if day not in self.calendar:
                self.gaps.append({'date': day.isoformat(), 'kind': 'calendar_day_missing', 'code': 'MARKET'})
            for code in sorted(codes):
                if not self.covered(day, code, 'actions'):
                    self.gaps.append({'date': day.isoformat(), 'kind': 'actions_coverage_missing', 'code': code})

    def covered(self, day, code, kind):
        assertions = [row for row in self.coverage if row['code'] == code
                      and day_value(row['start_date']) <= day <= day_value(row['end_date'])]
        return bool(assertions) and all(row[kind + '_complete'] for row in assertions)

    def expected_exit(self, entry, months):
        if entry is None:
            return None, None
        target = add_months(entry, months)
        expected = first_session_after(target, self.calendar)
        if expected is not None and any(day not in self.calendar for day in _days(entry, expected)):
            expected = None
        return target, expected


def _days(start, end):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


class _Ledger:
    def __init__(self, members, market, published, as_of, months, bps, calculation_version):
        self.market, self.published, self.as_of = market, published, as_of
        self.months, self.rate = months, decimal_text(bps) / Decimal('10000')
        self.calculation_version = calculation_version
        self.positions = {}
        remaining = ONE
        for index, member in enumerate(members):
            allocation = remaining if index == len(members) - 1 else decimal_text(member['weight'])
            self.positions[member['code']] = _Position(member['weight'], allocation)
            remaining -= allocation
        self.empty_cash = remaining
        self.entries, self.curve = [], []
        self.gaps = list(market.gaps)
        self.cost, self.turnover = ZERO, ZERO
        self.entry = first_session_after(published, market.calendar) if published is not None else None
        self.target, self.expected = market.expected_exit(self.entry, months)

    @property
    def cash(self):
        return self.empty_cash + sum((position.cash for position in self.positions.values()), ZERO)

    @property
    def receivable(self):
        return sum((position.receivable for position in self.positions.values()), ZERO)

    @property
    def equity(self):
        return self.empty_cash + sum((position.equity for position in self.positions.values()), ZERO)

    def record(self, row, kind, cost=ZERO):
        self.entries.append({'event_id': row['event_id'], 'code': row['code'],
            'date': local_day(row).isoformat(), 'entry_kind': kind, 'sequence': len(self.entries) + 1,
            'horizon_months': self.months, 'shares_after': _text(self.positions[row['code']].shares),
            'cash_after': _text(self.cash), 'receivable_after': _text(self.receivable),
            'cost': _text(cost), 'calculation_version': self.calculation_version})

    def action(self, row):
        position = self.positions[row['code']]
        kind = row['type']
        if kind == 'split':
            factor = decimal_text(row['factor'])
            position.shares *= factor
            position.price /= factor
        elif kind == 'dividend_ex':
            amount = decimal_text(row['cash_per_share'])
            position.receivables[row['economic_event_id']] = dividend_receivable(position.shares, amount)
            # Until the same day's raw bar arrives, retain an ex-adjusted last mark.
            position.price = max(ZERO, position.price - amount)
        elif kind == 'dividend_paid':
            identity = self.market.ex_links[row['ex_event_id']]
            position.cash += position.receivables.pop(identity, ZERO)
        elif kind == 'delisting':
            position.delisted = True
            position.unresolved = row['settlement_status'] == 'unresolved' and position.shares > 0
            if row['settlement_status'] == 'settled':
                position.cash += position.shares * decimal_text(row['cash_per_share'])
                if position.shares:
                    position.exit_date = local_day(row)
                position.shares = ZERO
        elif kind == 'rights':
            kind = 'rights_not_subscribed'
        self.record(row, kind)

    def execute(self, day, session_number):
        for code, position in self.positions.items():
            if position.entry_status == 'pending' and session_number > 5:
                position.entry_status = 'unfilled'
            if not self.needs_bar(position, session_number):
                continue
            bar = self.market.bars[(code, day)]
            price = decimal_text(bar['open'])
            if position.entry_status == 'pending':
                if session_number <= 5 and not position.delisted and executable(bar, 'buy'):
                    position.shares = position.cash / (price * (ONE + self.rate))
                    notional = position.shares * price
                    fee = notional * self.rate
                    position.cash = ZERO  # The allocated cash includes friction and rounding residue.
                    position.price = price
                    position.purchase_date, position.entry_status = day, 'filled'
                    self.cost += fee
                    self.turnover += notional
                    self.record(bar, 'buy', fee)
                elif session_number >= 5:
                    position.entry_status = 'unfilled'
            if (self.expected is not None and day >= self.expected and position.shares > 0
                    and not position.delisted and executable(bar, 'sell', position.purchase_date)):
                notional = position.shares * price
                fee = notional * self.rate
                position.cash += notional - fee
                position.shares = ZERO
                position.exit_date = day
                self.cost += fee
                self.turnover += notional
                self.record(bar, 'sell', fee)
        for code, position in self.positions.items():
            if not position.delisted and position.shares:
                bar = self.market.bars[(code, day)]
                position.price = decimal_text(bar['close'])
                self.record(bar, 'mark')

    @staticmethod
    def needs_bar(position, session_number):
        return not position.delisted and (position.shares > 0
            or (position.entry_status == 'pending' and session_number <= 5))

    def run(self):
        if self.published is not None:
            sessions = 0
            first_gap = min((day_value(gap['date']) for gap in self.market.gaps), default=None)
            for day in _days(self.published, self.as_of):
                if first_gap is not None and day >= first_gap:
                    break  # No invented fills, actions or flat prices across unknown history.
                actions = self.market.actions[day]
                for row in actions:
                    if utc_time(row['effective_at']).astimezone(SHANGHAI).time() <= time(9, 30):
                        self.action(row)
                if self.entry is not None and day >= self.entry and self.market.calendar[day]:
                    sessions += 1
                    for code, position in self.positions.items():
                        if self.needs_bar(position, sessions):
                            if not self.market.covered(day, code, 'bars'):
                                self.gaps.append({'date': day.isoformat(), 'kind': 'bars_coverage_missing', 'code': code})
                            if (code, day) not in self.market.bars:
                                self.gaps.append({'date': day.isoformat(), 'kind': 'bar_missing', 'code': code})
                    if len(self.gaps) > len(self.market.gaps):
                        break
                    self.execute(day, sessions)
                for row in actions:
                    if utc_time(row['effective_at']).astimezone(SHANGHAI).time() > time(9, 30):
                        self.action(row)
                self.curve.append({'date': day.isoformat(), 'equity': _text(self.equity),
                                   'cash': _text(self.cash), 'receivable': _text(self.receivable)})
        return self.result()

    def result(self):
        unresolved = any(position.unresolved for position in self.positions.values())
        holding = any(position.shares > 0 for position in self.positions.values())
        matured = self.expected is not None and self.as_of >= self.expected
        if not self.positions:
            state = 'empty_cohort'
        elif self.published is None:
            state = 'entry_time_unavailable'
        elif self.gaps:
            state = 'data_incomplete'
        elif self.entry is None or self.entry > self.as_of:
            state = 'pending_entry'
        elif unresolved:
            state = 'unresolved_delisting'
        elif not matured:
            state = 'pending_maturity'
        elif holding:
            state = 'exit_pending'
        else:
            state = 'completed'
        exits = [position.exit_date for position in self.positions.values() if position.exit_date]
        actual = max(exits) if exits and not holding else None
        terminal = max(self.expected, actual or self.expected) if state == 'completed' else None
        valid = state not in ('empty_cohort', 'entry_time_unavailable', 'data_incomplete', 'pending_entry')
        net = equity_return(ONE, self.equity) if valid else None
        last_session = max((day for day, opened in self.market.calendar.items()
                            if opened and self.entry is not None and self.entry <= day <= self.as_of), default=None)
        return {'horizon_months': self.months, 'horizon_status': state,
            'entry_target_date': self.entry.isoformat() if self.entry else None,
            'target_date': self.target.isoformat() if self.target else None,
            'expected_exit_date': self.expected.isoformat() if self.expected else None,
            'actual_exit_date': actual.isoformat() if actual else None,
            'terminal_date': terminal.isoformat() if terminal else None,
            'observation_date': last_session.isoformat() if last_session else None,
            'exit_status': 'unresolved_delisting' if unresolved else (
                'completed' if state == 'completed' else 'exit_pending' if matured and holding else 'not_due'),
            'horizon_return': _text(net) if state == 'completed' else None,
            'net_return': _text(net) if net is not None else None,
            'cash_realized_return': _text(equity_return(ONE, self.cash)) if valid and not holding else None,
            'equity': _text(self.equity), 'cash': _text(self.cash), 'receivable': _text(self.receivable),
            'zero_recovery_equity': _text(self.equity - sum((p.shares * p.price for p in self.positions.values()
                                                           if p.unresolved), ZERO)),
            'trading_cost': _text(self.cost), 'turnover': _text(self.turnover),
            'entry_coverage': _text(Decimal(sum(p.purchase_date is not None for p in self.positions.values()))
                                    / len(self.positions)) if self.positions else None,
            'exit_completion_rate': _text(Decimal(sum(p.exit_date is not None for p in self.positions.values()))
                                         / sum(p.purchase_date is not None for p in self.positions.values()))
                                    if any(p.purchase_date is not None for p in self.positions.values()) else None,
            'max_drawdown': _text(max_drawdown([ONE] + [Decimal(point['equity']) for point in self.curve]))
                            if valid else None,
            'positions': {code: {'initial_weight': p.weight, 'shares': _text(p.shares),
                'cash': _text(p.cash), 'receivable': _text(p.receivable), 'last_price': _text(p.price),
                'purchase_date': p.purchase_date.isoformat() if p.purchase_date else None,
                'exit_date': p.exit_date.isoformat() if p.exit_date else None, 'entry_status': p.entry_status,
                'unresolved_delisting': p.unresolved} for code, p in self.positions.items()},
            'ledger': self.entries, 'equity_curve': self.curve, 'data_gaps': self.gaps,
        }


def _benchmarks(result, market, identities):
    output = {}
    for role, identity in identities.items():
        end_date = result['terminal_date'] or result['observation_date']
        end_point = 'session_open' if result['terminal_date'] else 'session_close'
        endpoints = []
        for day, point in ((result['entry_target_date'], 'session_open'), (end_date, end_point)):
            matches = [row for row in market.benchmarks if local_day(row).isoformat() == day
                       and row['index_id'] == identity['id'] and row['source_id'] == identity['source']
                       and row['valuation_point'] == point]
            endpoints.append(matches[0] if len(matches) == 1 else None)
        available = all(endpoints) and result['net_return'] is not None
        item = {'id': identity['id'], 'source': identity['source'],
                'status': 'available' if available else 'benchmark_unavailable',
                'entry_date': result['entry_target_date'], 'entry_valuation_point': 'session_open',
                'end_date': end_date, 'end_valuation_point': end_point,
                'return': None, 'relative_return': None, 'event_ids': [], 'series_revisions': []}
        if available:
            start, end = endpoints
            value = equity_return(decimal_text(start['total_return_level']), decimal_text(end['total_return_level']))
            item.update({'return': _text(value), 'relative_return': _text(Decimal(result['net_return']) - value),
                         'event_ids': [start['event_id'], end['event_id']],
                         'series_revisions': sorted({start['series_revision'], end['series_revision']})})
        output[role] = item
    return output


def simulate_cohort(manifest: Mapping[str, object], events: Sequence[Mapping[str, object]],
                    as_of: date) -> Mapping[str, object]:
    """Replay immutable membership under sourced market observations known by as_of.

    An incomplete input returns a labelled known prefix, never a completed return.
    Every persisted financial number is decimal text; calendar dates remain dates.
    T16 persists only the default-cost ledger, plus observation sensitivity metrics.
    """
    with localcontext(Context(prec=34)):
        config, members, published = _manifest(manifest)
        selected = select_events(events, as_of)
        event_digest = digest(selected)
        calculation_version = ENGINE_VERSION + ':' + event_digest
        market = _Market([row for row in selected if not row.get('withdrawn', False)],
                         events, {member['code'] for member in members}, published, as_of)
        horizons = {}
        for months in (3, 6, 12):
            costs = set(config['sensitivity_bps']) | {config['cost_bps_each_side'], '0'}
            paths = {bps: _Ledger(members, market, published, as_of, months, bps,
                                 calculation_version).run() for bps in sorted(costs, key=Decimal)}
            result = paths[config['cost_bps_each_side']]
            result['gross_return'] = paths['0']['net_return']
            result['gross_horizon_return'] = paths['0']['horizon_return']
            result['cost_sensitivity'] = {bps: {key: paths[bps][key] for key in
                ('net_return', 'horizon_return', 'trading_cost', 'turnover')} for bps in config['sensitivity_bps']}
            result['benchmarks'] = _benchmarks(result, market, manifest['protocol']['benchmarks'])
            horizons[str(months)] = result
        return {'engine_version': ENGINE_VERSION, 'calculation_version': calculation_version,
                'event_digest': event_digest, 'selected_event_ids': [row['event_id'] for row in selected],
                'as_of': as_of.isoformat(), 'initial_equity': '1', 'currency': 'CNY',
                'manifest_hash': manifest['manifest_hash'], 'protocol_hash': manifest['protocol_hash'],
                'market_policy': dict(MARKET_POLICY), 'decimal_precision': 34,
                'allocation_policy': 'frozen_equal_weights_sorted_last_code_residual',
                'limitations': ['fractional_paper_shares', 'daily_bars_do_not_prove_open_liquidity',
                                'normalized_assertions_require_upstream_provenance_verification'],
                'horizons': horizons}
