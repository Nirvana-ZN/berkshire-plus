"""Versioned declared scenarios and deterministic prices, separate from source facts."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException, localcontext
from hashlib import sha256
import json

from sqlalchemy import select

from discovery.constants import CHINA_MARKET_TIMEZONE
from discovery.metrics import _context, _working_precision


VERSION = 'valuation-scenarios-v1'
POLICY = {'version': VERSION, 'currency': 'CNY', 'shares_unit': 'share',
          'sensitivity_growth_step': '0.01', 'treasury_max_age_days': 30,
          'current_shares': 'cutoff_shanghai_day', 'rounding': 'ROUND_HALF_EVEN',
          'precision_ceiling': 4096, 'decimal_exponent_limit': 4096,
          'per_share_division': 'discovery.metrics._working_precision:minimum34,extra32',
          'cyclic_normalization': 'selection_contract.annual_values:five_consecutive_annual_median'}
LABELS = {'low': '积极买入价', 'base': '常规买入价', 'high': '停止加仓价'}
FORMULA = 'metric_per_share * (1 + growth) ** horizon_years * multiple * (1 - haircut)'


@dataclass(frozen=True)
class ValuationAssumptions:
    method: str
    metric_per_share: Decimal
    growth: Decimal
    multiples: tuple[Decimal, Decimal, Decimal]
    haircuts: tuple[Decimal, Decimal, Decimal]
    horizon_years: int
    evidence_ids: tuple[str, ...]


def _text(value: Decimal) -> str:
    if not _in_range(value):
        raise ValueError('valuation decimal text range exceeded')
    if value == 0:
        return '0'
    raw = format(value, 'f')
    return raw.rstrip('0').rstrip('.') if '.' in raw else raw


def _finite(value):
    return isinstance(value, Decimal) and value.is_finite()


def _in_range(value):
    return (_finite(value) and abs(value.adjusted()) <= POLICY['decimal_exponent_limit']
        and abs(value.as_tuple().exponent) <= POLICY['decimal_exponent_limit']
        and len(value.as_tuple().digits) <= POLICY['precision_ceiling'])


def _precision(a):
    if (not isinstance(a, ValuationAssumptions) or not isinstance(a.method, str) or a.method not in {'pe', 'pb'}
            or not _finite(a.metric_per_share) or not _finite(a.growth)
            or a.metric_per_share <= 0 or a.growth <= -1):
        raise ValueError('invalid valuation metric')
    if (type(a.horizon_years) is not int or a.horizon_years < 1
            or not isinstance(a.evidence_ids, tuple) or not a.evidence_ids
            or any(not isinstance(key, str) or not key.strip() for key in a.evidence_ids)):
        raise ValueError('missing valuation basis')
    if (not isinstance(a.multiples, tuple) or not isinstance(a.haircuts, tuple)
            or len(a.multiples) != 3 or len(a.haircuts) != 3
            or any(not _finite(m) or m <= 0 for m in a.multiples)
            or any(not _finite(h) or not 0 <= h < 1 for h in a.haircuts)
            or not a.multiples[0] < a.multiples[1] < a.multiples[2]):
        raise ValueError('invalid valuation assumptions')
    values = (a.metric_per_share, a.growth, *a.multiples, *a.haircuts)
    if any(not _in_range(value) for value in values):
        raise ValueError('valuation numeric range exceeded')
    # Reserve product/power coefficient space; never inherit ambient precision.
    extra = sum(len(v.as_tuple().digits) for v in values) + a.horizon_years * (
        len(a.growth.as_tuple().digits) + abs(a.growth.as_tuple().exponent) + 2)
    precision = _working_precision(values, extra=extra)
    if precision is None:
        raise ValueError('valuation numeric range exceeded')
    return precision


def price_bands(a: ValuationAssumptions) -> tuple[Decimal, Decimal, Decimal]:
    precision = _precision(a)
    try:
        with localcontext(_context(precision)):
            result = tuple(a.metric_per_share * (1 + a.growth) ** a.horizon_years * m * (1 - h)
                           for m, h in zip(a.multiples, a.haircuts, strict=True))
        if any(not _in_range(value) or value <= 0 for value in result) or not result[0] < result[1] < result[2]:
            raise ValueError('invalid price band order')
        return result
    except DecimalException as error:
        raise ValueError('valuation numeric range exceeded') from error


def valuation_report(a: ValuationAssumptions, current_price: Decimal | None) -> Mapping[str, object]:
    values = price_bands(a)
    if current_price is not None and (not _finite(current_price) or current_price <= 0):
        raise ValueError('invalid current price')
    sensitivity = []
    with localcontext(_context(_precision(a))):
        for delta in (Decimal('-0.01'), Decimal('0'), Decimal('0.01')):
            growth = a.growth + delta
            for multiple in a.multiples:
                price = (a.metric_per_share * (1 + growth) ** a.horizon_years * multiple
                         * (1 - a.haircuts[1])) if growth > -1 else None
                sensitivity.append({'growth': _text(growth), 'multiple': _text(multiple),
                                    'haircut': _text(a.haircuts[1]),
                                    'price': _text(price) if price is not None else None})
    return {'version': VERSION, 'policy': dict(POLICY), 'formula': FORMULA,
        'method': a.method, 'current_price': _text(current_price) if current_price is not None else None,
        'labels': dict(LABELS), 'computed_prices': dict(zip(('low', 'base', 'high'), map(_text, values), strict=True)),
        'declared_assumptions': {'metric_per_share': _text(a.metric_per_share), 'growth': _text(a.growth),
            'multiples': list(map(_text, a.multiples)), 'haircuts': list(map(_text, a.haircuts)),
            'horizon_years': a.horizon_years, 'evidence_ids': list(a.evidence_ids)},
        'pessimistic': {'price': _text(values[0]), 'growth': _text(a.growth),
            'multiple': _text(a.multiples[0]), 'haircut': _text(a.haircuts[0]),
            'description': '低倍数及其折扣情景，不代表精确公允价值'},
        'sensitivity': sensitivity}


def _now():
    return datetime.now(timezone.utc)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode('utf-8')).hexdigest()


def _decimal(value):
    if not isinstance(value, str):
        raise ValueError('assumption financial values require decimal text')
    try:
        result = Decimal(value)
        if not _in_range(result) or _working_precision((result,)) is None:
            raise ValueError('invalid assumption decimal')
        return result
    except DecimalException as error:
        raise ValueError('invalid assumption decimal') from error


def assumptions_from_payload(payload: Mapping[str, object]) -> ValuationAssumptions:
    """The sole persisted named-field -> tuple DTO boundary."""
    try:
        result = ValuationAssumptions(payload['method'], _decimal(payload['normalized_metric_per_share']),
            _decimal(payload['earnings_growth']),
            tuple(_decimal(payload['multiple_' + key]) for key in ('low', 'base', 'high')),
            tuple(_decimal(payload['haircut_' + key]) for key in ('low', 'base', 'high')),
            payload.get('horizon_years', 1), tuple(payload['evidence_ids']))
        price_bands(result)
        return result
    except (KeyError, TypeError) as error:
        raise ValueError('missing valuation assumptions') from error


def _normalize(payload):
    if not isinstance(payload, Mapping):
        raise ValueError('assumption payload must be an object')
    allowed = {'method', 'normalized_metric_per_share', 'earnings_growth', 'multiple_low', 'multiple_base',
        'multiple_high', 'haircut_low', 'haircut_base', 'haircut_high', 'horizon_years', 'evidence_ids',
        'metric_basis', 'period', 'profile_evidence_id', 'shares_evidence_id', 'price_evidence_id',
        'treasury_evidence_id', 'equity_evidence_id', 'bank_adjustment', 'rationale', 'review'}
    if set(payload) - allowed:
        raise ValueError('unknown assumption fields')
    a = assumptions_from_payload(payload)
    result = dict(payload)
    for key in ('normalized_metric_per_share', 'earnings_growth', 'multiple_low', 'multiple_base',
                'multiple_high', 'haircut_low', 'haircut_base', 'haircut_high'):
        result[key] = _text(_decimal(result[key]))
    result['evidence_ids'] = sorted(set(a.evidence_ids))
    result['horizon_years'] = a.horizon_years
    for key in ('rationale', 'metric_basis', 'period', 'profile_evidence_id', 'shares_evidence_id'):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise ValueError(f'missing assumption {key}')
    review = result.get('review')
    if (not isinstance(review, Mapping) or set(review) != {'method', 'reviewer', 'reviewed_at'}
            or review.get('method') != 'human_review' or not isinstance(review.get('reviewer'), str)
            or not review['reviewer'].strip()):
        raise ValueError('explicit assumption human review required')
    try:
        reviewed = datetime.fromisoformat(review['reviewed_at'])
        if reviewed.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError('assumption review requires an aware time') from error
    result['review'] = {**review, 'reviewed_at': reviewed.astimezone(timezone.utc).isoformat()}
    # Also rejects non-JSON extras and retains a plain immutable JSON boundary.
    return json.loads(_json(result))


def _calculate(payload, records, sources, *, code, cutoff):
    """Verify selected facts and derive the metric, never a model-provided fallback."""
    from discovery.fundamentals import _facts_calculation, freeze_fundamentals, period_order, ttm_amount
    from discovery.industry import classify_industry_branch
    from discovery.selection_contract import annual_values

    a = assumptions_from_payload(payload)
    by_id = {row.evidence_id: row for row in records}
    ids = set(a.evidence_ids)
    if any(key not in by_id or by_id[key].code != code or by_id[key].status != 'verified'
           or sources.get(key, {}).get('status') != 'verified' for key in ids):
        raise ValueError('valuation evidence missing, superseded or unverified')
    if datetime.fromisoformat(payload['review']['reviewed_at']) > cutoff:
        raise ValueError('assumption review follows cutoff')

    def selected(key, kind):
        identity = payload.get(key)
        if identity not in ids or by_id[identity].kind != kind:
            raise ValueError(f'invalid {key}')
        return by_id[identity]

    profile = selected('profile_evidence_id', 'company_profile')
    branch = classify_industry_branch(profile.values.get('_industry'))
    # Explicit assumption IDs select a source, not an industry interpretation.
    # All authoritative active sources in the frozen set must agree, including
    # profiles the caller omitted from its declared scenario evidence IDs.
    profiles = tuple(row for row in records if row.kind == 'company_profile' and row.status == 'verified')
    # A status-only correction may have hidden an earlier profile during source
    # selection. Its missing retained proof cannot silently resolve authority.
    if any(sources.get(row.evidence_id, {}).get('status') != 'verified' for row in profiles):
        raise ValueError('company profile retained proof incomplete')
    branches = {classify_industry_branch(row.values.get('_industry')) for row in profiles}
    if branches != {branch}:
        raise ValueError('conflicting authoritative company profiles')
    if (branch, a.method) not in {('bank', 'pb'), ('nonfinancial', 'pe')}:
        raise ValueError('industry method mismatch or unsupported industry')
    shares = selected('shares_evidence_id', 'share_snapshot')
    day = cutoff.astimezone(CHINA_MARKET_TIMEZONE).date()
    if shares.period != day.isoformat():
        raise ValueError('current share snapshot day mismatch')
    count = _decimal(shares.values.get('shares'))
    if (count <= 0 or shares.basis_id.casefold().startswith(('unknown', 'unverified'))
            or shares.values.get('_basis_status') in {'unknown', 'unverified'}):
        raise ValueError('current shares or basis invalid')
    if any(row.kind == 'share_snapshot' and row.period == shares.period and row.status == 'verified'
           and (row.basis_id != shares.basis_id or _decimal(row.values.get('shares')) != count)
           for row in records):
        raise ValueError('conflicting current share snapshots')
    financial = tuple(row for row in records if row.kind == 'financial')
    period = payload['period']
    if not financial or period != max((row.period for row in financial), key=period_order):
        raise ValueError('valuation requires latest frozen financial period')
    basis = payload['metric_basis']
    if a.method == 'pe' and basis == 'deducted_ttm':
        metric = ttm_amount(financial, 'deducted_net_income', period)
    elif a.method == 'pe' and basis == 'normalized_deducted':
        end_year = int(period[:4]) if len(period) == 4 else int(period[:4]) - 1
        snapshot = freeze_fundamentals(records, (), (), code=code, cutoff_at=cutoff, current_mcap=None)
        metric = annual_values(snapshot, end_year, years=5)['normalized_earnings']
    elif a.method == 'pb' and basis == 'adjusted_bvps':
        equity = selected('equity_evidence_id', 'financial')
        explanation = payload.get('bank_adjustment')
        if (equity.period != period or not isinstance(explanation, str) or not explanation.strip()
                or explanation not in sources[equity.evidence_id]['excerpt']):
            raise ValueError('reviewed bank adjustment/basis explanation missing')
        metric = _facts_calculation(financial, (('adjusted_net_assets', period),), lambda value: value)
        if equity.evidence_id not in metric.evidence_ids:
            raise ValueError('adjusted bank equity source mismatch')
    else:
        raise ValueError('invalid metric basis')
    if (metric.status != 'verified' or metric.value <= 0 or not set(metric.evidence_ids) <= ids
            or any(by_id[key].basis_id != shares.basis_id for key in metric.evidence_ids)):
        raise ValueError('valuation metric incomplete, nonpositive or incomparable')
    derived = _facts_divide(metric.value, count)
    if a.metric_per_share != derived:
        raise ValueError('declared per-share metric differs from verified derivation: ' + _text(derived))
    price = None
    if payload.get('price_evidence_id') is not None:
        quote = selected('price_evidence_id', 'market_snapshot')
        if quote.period != day.isoformat():
            raise ValueError('current price snapshot day mismatch')
        price = _decimal(quote.values.get('price'))
        if price <= 0:
            raise ValueError('current price not positive')
        if any(row.kind == 'market_snapshot' and row.period == quote.period and row.status == 'verified'
               and _decimal(row.values.get('price')) != price for row in records):
            raise ValueError('conflicting current price snapshots')
    result = dict(valuation_report(a, price))
    result['source_facts'] = {'metric_basis': basis, 'period': period,
        'numerator': _text(metric.value), 'numerator_evidence_ids': list(metric.evidence_ids),
        'current_shares': _text(count), 'shares_evidence_id': shares.evidence_id,
        'basis_id': shares.basis_id, 'profile_evidence_id': profile.evidence_id,
        'price_evidence_id': payload.get('price_evidence_id')}
    spread = {'label': '估值利差', 'status': 'pending', 'reason': 'treasury_source_missing',
              'value': None, 'treasury_source': None, 'treasury_date': None}
    if payload.get('treasury_evidence_id') is not None:
        treasury = selected('treasury_evidence_id', 'treasury_yield')
        treasury_day = date.fromisoformat(treasury.period)
        rate = _decimal(treasury.values.get('yield'))
        if not 0 <= rate < 1 or treasury_day > day:
            raise ValueError('invalid treasury yield/date')
        spread.update(treasury_source=treasury.evidence_id, treasury_date=treasury.period,
                      treasury_yield=_text(rate), source_url=treasury.source_url)
        if (day - treasury_day).days > POLICY['treasury_max_age_days']:
            spread['reason'] = 'treasury_source_stale'
        elif a.method != 'pe':
            spread['reason'] = 'pe_only'
        elif price is None:
            spread['reason'] = 'current_price_missing'
        else:
            with localcontext(_context(_precision(a))):
                spread.update(status='available', reason=None, value=_text(a.metric_per_share / price - rate))
    result['valuation_spread'] = spread
    return result


def _facts_divide(numerator, denominator):
    precision = _working_precision((numerator, denominator))
    if precision is None:
        raise ValueError('per-share numeric range exceeded')
    with localcontext(_context(precision)):
        return numerator / denominator


def _version(user_id, code, payload, records):
    by_id = {row.evidence_id: row for row in records}
    return {'version': VERSION, 'policy': dict(POLICY), 'user_id': user_id, 'code': code,
        'payload': payload, 'source_summary': [
            {'evidence_id': key, 'document_sha256': by_id[key].document_sha256,
             'source_url': by_id[key].source_url, 'period': by_id[key].period,
             'basis_id': by_id[key].basis_id} for key in payload['evidence_ids']]}


def append_assumptions(session, *, user_id, code, payload, dry_run=False):
    """Validate locally reviewed inputs against retained sources, then append only."""
    from app.models import User
    from app.selection_models import ResearchAssumption
    from discovery.evidence_store import load_evidence_as_of
    from research.evidence import freeze_research_context, read_research_context

    if type(user_id) is not int or session.get(User, user_id) is None:
        raise ValueError('assumption owner missing')
    now = _now()
    data = _normalize(payload)
    context = freeze_research_context(load_evidence_as_of(session, code, now), None, now)
    records = read_research_context(context)
    _calculate(data, records, context['sources'], code=code, cutoff=now)
    version = _version(user_id, code, data, records)
    digest = _digest(version)
    if dry_run:
        return digest
    existing = session.scalar(select(ResearchAssumption).where(ResearchAssumption.digest == digest))
    if existing is not None:
        return existing.id
    row = ResearchAssumption(user_id=user_id, code=code, created_at=now, method=data['method'],
        payload_json=_json(version), evidence_ids_json=_json(data['evidence_ids']), digest=digest)
    session.add(row)
    session.flush()
    return row.id


def freeze_assumptions(session, *, assumption_id, user_id, code, context):
    """Select an explicit owned version before the creation-time context is sealed."""
    from app.selection_models import ResearchAssumption

    if type(assumption_id) is not int or assumption_id <= 0:
        raise ValueError('assumption_id must be a positive integer')
    row = session.get(ResearchAssumption, assumption_id)
    if row is None or row.user_id != user_id or row.code != code:
        raise ValueError('assumption owner/company/version mismatch')
    version = json.loads(row.payload_json)
    if (row.digest != _digest(version) or version.get('user_id') != user_id or version.get('code') != code
            or version.get('version') != VERSION or version.get('policy') != POLICY
            or version['payload']['method'] != row.method
            or version['payload']['evidence_ids'] != json.loads(row.evidence_ids_json)):
        raise ValueError('assumption version integrity mismatch')
    frozen = {**version, 'assumption_id': row.id, 'digest': row.digest,
              'created_at': row.created_at.isoformat()}
    frozen['validation'] = validate_frozen_assumptions(frozen, context, code=code, user_id=user_id)
    return frozen


def validate_frozen_assumptions(frozen, context, *, code, user_id=1):
    """Replay from the immutable context only; never query DB/files or call LLM."""
    from research.evidence import read_research_context

    if not isinstance(frozen, Mapping):
        return {'complete': False, 'reason': 'validated_valuation_unavailable', 'report': None}
    try:
        records = read_research_context(context)
        cutoff = datetime.fromisoformat(context['cutoff_at'])
        version = {key: value for key, value in frozen.items() if key not in {'assumption_id', 'digest', 'created_at', 'validation'}}
        if (type(frozen.get('assumption_id')) is not int or frozen['assumption_id'] <= 0
                or context.get('assumption_id') not in (None, frozen['assumption_id'])
                or frozen.get('user_id') != user_id or frozen.get('code') != code
                or frozen.get('version') != VERSION or frozen.get('policy') != POLICY
                or frozen.get('digest') != _digest(version)
                or datetime.fromisoformat(frozen['created_at']) > cutoff):
            raise ValueError('assumption identity, policy or cutoff invalid')
        data = _normalize(frozen['payload'])
        if data != frozen['payload'] or _version(user_id, code, data, records) != version:
            raise ValueError('assumption sources changed')
        report = _calculate(data, records, context['sources'], code=code, cutoff=cutoff)
        report.update(assumption_id=frozen['assumption_id'], assumption_digest=frozen['digest'],
                      reviewed_assumptions=data)
        return {'complete': True, 'reason': None, 'report': report}
    except (ValueError, KeyError, TypeError, DecimalException) as error:
        return {'complete': False, 'reason': str(error), 'report': None}
