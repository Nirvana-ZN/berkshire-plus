"""Versioned registration protocol, independent of future market observations."""

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json


VARIANTS = ('full_selected', 'no_operating_selected', 'all_passed')
OPERATING_FLAGS = ('interim_deterioration', 'operating_unknown', 'annual_operating_deterioration')


def default_tracking_config() -> dict[str, object]:
    return {
        'version': 'paper-v1', 'horizons_months': [3, 6, 12],
        'entry': 'next_full_session_open', 'max_entry_sessions': 5,
        'unfilled': 'retain_cash', 'round_lots': False, 'cash_rate': '0',
        'cost_bps_each_side': '15', 'sensitivity_bps': ['0', '15', '30', '50'],
        'primary_benchmark': 'CSI300_TOTAL_RETURN',
        'secondary_benchmark': 'CSI500_TOTAL_RETURN',
        'rights_subscription': False,
        'dividend_policy': 'ex_date_receivable_pay_date_cash',
    }


def canonical_json(value: object) -> str:
    """Canonical protocol JSON never accepts binary floating point values."""
    def plain(item):
        if item is None or type(item) in (str, int, bool):
            return item
        if isinstance(item, Mapping) and all(type(key) is str for key in item):
            return {key: plain(value) for key, value in item.items()}
        if isinstance(item, (tuple, list)):
            return [plain(value) for value in item]
        raise ValueError('registration JSON requires exact text/integer values')
    return json.dumps(plain(value), sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def utc_timestamp(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('registration requires an aware datetime')
    return value.astimezone(timezone.utc)


def validated_config(config: Mapping[str, object]) -> dict[str, object]:
    defaults = default_tracking_config()
    if not isinstance(config, Mapping) or set(config) != set(defaults):
        raise ValueError('invalid tracking config keys')
    value = json.loads(canonical_json(config))
    # paper-v1 supports a separately registered cost or benchmark choice;
    # changing execution semantics requires a new implemented protocol version.
    variable = {'cost_bps_each_side', 'primary_benchmark', 'secondary_benchmark'}
    for key in defaults.keys() - variable:
        if canonical_json(value[key]) != canonical_json(defaults[key]):
            raise ValueError(f'unsupported tracking config: {key}')
    cost = value['cost_bps_each_side']
    try:
        amount = Decimal(cost) if type(cost) is str else None
        if amount is None or not amount.is_finite() or not 0 <= amount < 10000:
            raise ValueError
    except (ValueError, InvalidOperation):
        raise ValueError('tracking config cost must be finite decimal text in [0,10000)') from None
    cost_text = format(amount, 'f')
    value['cost_bps_each_side'] = cost_text.rstrip('0').rstrip('.') if '.' in cost_text else cost_text
    if amount == 0:
        value['cost_bps_each_side'] = '0'
    for key in ('primary_benchmark', 'secondary_benchmark'):
        if value[key] not in ('CSI300_TOTAL_RETURN', 'CSI500_TOTAL_RETURN'):
            raise ValueError('tracking config benchmark identity is unsupported')
    return value


def protocol(config: Mapping[str, object]) -> dict[str, object]:
    """Freeze identities and rules; later series bytes and realized dates are separate."""
    return {
        'version': 'paper-registration-v1', 'config': dict(config), 'variants': list(VARIANTS),
        'entry_policy': {
            'rule': config['entry'], 'calendar_timezone': 'Asia/Shanghai',
            'same_publication_day_allowed': False, 'suspension': 'unfilled',
            'open_limit_up': 'unfilled', 'max_entry_sessions': config['max_entry_sessions'],
            'unfilled': config['unfilled'], 'round_lots': config['round_lots'],
        },
        'cost_policy': {'bps_each_side': config['cost_bps_each_side'],
                        'sensitivity_bps': config['sensitivity_bps'], 'kind': 'experiment_assumption'},
        'exit_policy': {
            'horizons_months': config['horizons_months'], 'anchor': 'entry_target_date',
            'calendar_months': 'clip_month_end', 'session': 'first_session_after_target',
            'earliest_sale': 'session_after_purchase', 'suspension': 'exit_pending',
            'open_limit_down': 'exit_pending', 'independent_horizon_ledgers': True,
        },
        'benchmarks': {role: {
            'id': config[key], 'source': 'China Securities Index Co., Ltd.',
            'return_convention': 'total_return', 'definition_version': 'paper-benchmark-identity-v1',
            'revision_policy': 'retain_series_source_and_revision_per_observation',
            'series_status': 'not_observed',
        } for role, key in (('primary', 'primary_benchmark'), ('auxiliary', 'secondary_benchmark'))},
        'ablation_policy': {
            'version': 'remove-operating-layer-v1', 'operating_state_projection': 'stable',
            'removed_operating_flags': list(OPERATING_FLAGS),
            'other_risks': 'preserve', 'metrics_and_caps': 'unchanged',
        },
        'weight_policy': {'rule': 'equal_original_members', 'decimal_precision': 34,
                          'unfilled': 'retain_original_weight_as_cash'},
        'cross_track_aggregation': 'deduplicate_code_only_in_summary',
        'competition_exclusion': 'not_a_sell_signal',
    }
