"""Exact common-risk arithmetic and locally reviewed, append-only selections.

Labels are human interpretations of retained revenue/holdings evidence. Source
proof verifies the cited bytes and review, not the truth of that interpretation.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
from types import MappingProxyType

from sqlalchemy import select

from discovery.constants import CHINA_MARKET_TIMEZONE


VERSION = 'economic-exposure-v1'
LABELS = {'property': '地产/基建需求', 'exports': '出口与汇率', 'ai_capex': 'AI资本开支',
          'consumption': '消费需求', 'commodities': '商品价格', 'rates': '利率'}
RULES = {'version': VERSION, 'aggregation': 'full_weight_per_distinct_label',
         'unknown': 'unmapped_or_unusable', 'core_cap': '0.15', 'satellite_cap': '0.08',
         'priority': ['exit_review', 'freeze_add', 'eligible'],
         'addition_verdicts': ['pass'], 'addition_grades': ['A', 'B'],
         'addition_states': ['stable', 'improving'],
         'breach_rule': 'all_positive_deltas_together', 'unweighted_notes': 'not_authorization'}
ZERO = Decimal('0')


def _decimal(value, *, signed=False):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError('weight/limit must be a finite Decimal')
    if (not signed and value < 0) or abs(value.adjusted()) > 4096 or len(value.as_tuple().digits) > 4096:
        raise ValueError('weight/limit out of bounds')
    return value


def _number(value):
    if not isinstance(value, str):
        raise ValueError('financial input must be decimal text')
    try:
        return _decimal(Decimal(value))
    except InvalidOperation as error:
        raise ValueError('invalid decimal text') from error


def _text_decimal(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


@dataclass(frozen=True)
class ExposurePolicy:
    limits: Mapping[str, Decimal]
    unknown_limit: Decimal | None = None

    def __post_init__(self):
        if not isinstance(self.limits, Mapping) or any(key not in LABELS for key in self.limits):
            raise ValueError('unsupported exposure limit')
        limits = {key: _decimal(value) for key, value in self.limits.items()}
        if self.unknown_limit is not None:
            _decimal(self.unknown_limit)
        if any(value > 1 for value in [*limits.values(), *([] if self.unknown_limit is None else [self.unknown_limit])]):
            raise ValueError('limits must be fractions between zero and one')
        object.__setattr__(self, 'limits', MappingProxyType(limits))


def exposure_totals(weights: Mapping[str, Decimal], labels: Mapping[str, tuple[str, ...]]) -> dict[str, Decimal]:
    result = {'unknown': ZERO}
    # Private precision covers the full accepted exponent span, independent of
    # ambient caller precision. Financial arithmetic never passes through float.
    with localcontext() as context:
        context.prec = 12300 + len(str(len(weights) or 1))
        for code, weight in weights.items():
            _decimal(weight)
            tags = labels.get(code, ())
            if isinstance(tags, str) or any(tag not in LABELS for tag in tags):
                raise ValueError('unsupported exposure label')
            for tag in set(tags) or {'unknown'}:
                result[tag] = result.get(tag, ZERO) + weight
    return result


def check_proposed_exposure(current_weights, delta_weights, labels, policy):
    current = exposure_totals(current_weights, labels)
    proposed = dict(current_weights)
    with localcontext() as context:
        context.prec = 12300 + len(str(len(current_weights) + len(delta_weights) or 1))
        for code, delta in delta_weights.items():
            _decimal(delta, signed=True)
            proposed[code] = proposed.get(code, ZERO) + delta
        totals = exposure_totals(proposed, labels)
        limits = dict(policy.limits)
        if policy.unknown_limit is not None:
            limits['unknown'] = policy.unknown_limit
        breaches = {tag: totals.get(tag, ZERO) - limit for tag, limit in limits.items()
                    if totals.get(tag, ZERO) > limit}
    return {'allowed': not breaches or not any(delta > 0 for delta in delta_weights.values()),
            'current_totals': current, 'proposed_totals': totals, 'breaches': breaches}


def proposal_priority(verdict: str, operating_state: str, grade: str) -> str:
    if str(verdict).lower() in {'fail', 'skip'}:
        return 'exit_review'
    if verdict != 'pass' or grade not in {'A', 'B'} or operating_state not in {'stable', 'improving'}:
        return 'freeze_add'
    return 'eligible'


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode('utf-8')).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _time(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if result.utcoffset() is not None:
            return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        pass
    raise ValueError('an aware timestamp is required')


def _required(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be nonempty text')
    return value.strip()


def _policy(data):
    if (not isinstance(data, dict) or set(data) != {'limits', 'unknown_limit'}
            or not isinstance(data['limits'], dict)):
        raise ValueError('policy requires limits and unknown_limit')
    return ExposurePolicy({key: _number(value) for key, value in data['limits'].items()},
                          None if data['unknown_limit'] is None else _number(data['unknown_limit']))


def _policy_json(policy):
    return {'limits': {key: _text_decimal(value) for key, value in sorted(policy.limits.items())},
            'unknown_limit': None if policy.unknown_limit is None else _text_decimal(policy.unknown_limit)}


def _sources(session, code, cutoff):
    from discovery.evidence_store import load_evidence_as_of
    from research.evidence import freeze_research_context
    context = freeze_research_context(load_evidence_as_of(session, code, cutoff), None, cutoff)
    return {item['evidence_id']: item for item in context['model_context']['evidence']}


def _qualified(sources, ids, kinds, excerpt):
    if not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids):
        raise ValueError('explicit evidence IDs are required')
    chosen = []
    for key in sorted(set(ids)):
        item = sources.get(key)
        if (item is None or item['kind'] not in kinds or item['status'] != 'verified'
                or item['source_proof']['status'] != 'verified'):
            raise ValueError('evidence missing, superseded, wrong kind or unverified')
        chosen.append(item)
    if not any(excerpt in item['source_proof']['excerpt'] for item in chosen):
        raise ValueError('mapping/comparison basis must be a literal retained excerpt')
    return chosen


def _normalize(session, payload, now):
    if not isinstance(payload, dict) or set(payload) - {'policy', 'mappings', 'comparison', 'review'}:
        raise ValueError('invalid exposure version fields')
    review = payload.get('review', {})
    if (not isinstance(review, dict) or set(review) != {'method', 'reviewer', 'reviewed_at'}
            or review['method'] != 'human_review' or _time(review['reviewed_at']) > now):
        raise ValueError('an available human review is required')
    review = {'method': 'human_review', 'reviewer': _required(review['reviewer'], 'reviewer'),
              'reviewed_at': _time(review['reviewed_at']).isoformat()}
    policy = _policy_json(_policy(payload.get('policy')))
    mappings = payload.get('mappings')
    if not isinstance(mappings, dict):
        raise ValueError('mappings must be keyed by instrument code')
    normalized, evidence = {}, {}
    for code, raw in sorted(mappings.items()):
        _required(code, 'code')
        if not isinstance(raw, dict) or set(raw) != {'labels', 'effective_at', 'instrument_type',
                                                     'evidence_ids', 'basis_excerpt', 'rationale'}:
            raise ValueError('mapping fields are incomplete')
        tags = raw['labels']
        if not isinstance(tags, list) or not tags or any(tag not in LABELS for tag in tags):
            raise ValueError('mapping requires supported labels; omit unknown codes')
        if raw['instrument_type'] not in {'equity', 'etf'}:
            raise ValueError('instrument_type must be equity or etf')
        effective = _time(raw['effective_at'])
        excerpt = _required(raw['basis_excerpt'], 'basis_excerpt')
        chosen = _qualified(_sources(session, code, now), raw['evidence_ids'],
            {'fund_holdings'} if raw['instrument_type'] == 'etf' else
            {'segment_revenue', 'customer_revenue', 'geographic_revenue'}, excerpt)
        if _time(review['reviewed_at']) < max(_time(item['source_proof']['reviewed_at']) for item in chosen):
            raise ValueError('mapping review precedes source review')
        normalized[code] = {**raw, 'labels': sorted(set(tags)), 'effective_at': effective.isoformat(),
            'evidence_ids': sorted(set(raw['evidence_ids'])), 'basis_excerpt': excerpt,
            'rationale': _required(raw['rationale'], 'rationale')}
        evidence.update({item['evidence_id']: item for item in chosen})
    comparison = payload.get('comparison')
    if comparison is not None:
        required = {'code', 'as_of_date', 'evidence_ids', 'basis_excerpt', 'broad_market_basis',
                    'fee_rate', 'valuation_basis', 'valuation_multiple', 'earnings_assumption', 'confidence'}
        if not isinstance(comparison, dict) or set(comparison) != required:
            raise ValueError('ETF comparison fields are incomplete')
        code = _required(comparison['code'], 'ETF code')
        if code not in normalized or normalized[code]['instrument_type'] != 'etf':
            raise ValueError('ETF comparison requires sourced ETF common-risk mapping')
        chosen = _qualified(_sources(session, code, now), comparison['evidence_ids'],
                            {'etf_snapshot'}, _required(comparison['basis_excerpt'], 'basis_excerpt'))
        if _time(review['reviewed_at']) < max(_time(item['source_proof']['reviewed_at']) for item in chosen):
            raise ValueError('comparison review precedes source review')
        if any(item['period'] != comparison['as_of_date'] for item in chosen):
            raise ValueError('ETF facts must share the declared comparison date')
        from datetime import date
        date.fromisoformat(comparison['as_of_date'])
        if comparison['as_of_date'] > now.astimezone(CHINA_MARKET_TIMEZONE).date().isoformat():
            raise ValueError('ETF comparison date is in the future')
        for field in ('fee_rate', 'valuation_multiple'):
            value = _number(comparison[field])
            if field == 'fee_rate' and value >= 1 or field == 'valuation_multiple' and value <= 0:
                raise ValueError('ETF financial facts out of bounds')
            if not any(item['values'].get(field) is not None and _number(item['values'][field]) == value for item in chosen):
                raise ValueError('ETF financial fact differs from selected source')
        for field in ('broad_market_basis', 'valuation_basis'):
            literal = _required(comparison[field], field)
            if not any(literal in item['source_proof']['excerpt'] for item in chosen):
                raise ValueError('ETF basis must occur in a retained source')
        if comparison['confidence'] not in {'A', 'B', 'C'}:
            raise ValueError('ETF confidence must be A/B/C')
        comparison = {**comparison, 'evidence_ids': sorted(set(comparison['evidence_ids'])),
                      'earnings_assumption': _required(comparison['earnings_assumption'], 'earnings_assumption'),
                      'fee_rate': _text_decimal(_number(comparison['fee_rate'])),
                      'valuation_multiple': _text_decimal(_number(comparison['valuation_multiple']))}
        evidence.update({item['evidence_id']: item for item in chosen})
    return {'policy': policy, 'mappings': normalized, 'comparison': comparison, 'review': review}, evidence


def append_exposure_version(session, *, user_id, payload, supersedes_id=None, dry_run=False):
    from app.models import User
    from app.selection_models import ExposureVersion
    if type(user_id) is not int or session.get(User, user_id) is None:
        raise ValueError('exposure owner missing')
    now = _now()
    if supersedes_id is not None:
        parent = session.get(ExposureVersion, supersedes_id)
        if type(supersedes_id) is not int or parent is None or parent.user_id != user_id or parent.created_at > now:
            raise ValueError('supersedes version must belong to the owner')
    data, evidence = _normalize(session, payload, now)
    version = {'version': VERSION, 'rules': RULES, 'user_id': user_id,
               'supersedes_id': supersedes_id, 'payload': data, 'sources': evidence}
    digest = _digest(version)
    if dry_run:
        return digest
    existing = session.scalar(select(ExposureVersion).where(ExposureVersion.digest == digest))
    if existing is not None:
        return existing.id
    row = ExposureVersion(user_id=user_id, created_at=now, supersedes_id=supersedes_id,
                          payload_json=_json(version), digest=digest)
    session.add(row)
    session.flush()
    return row.id


def select_exposure_version(session, *, user_id, version_id, dry_run=False):
    from app.selection_models import ExposureVersion, ExposureSelection
    row = session.get(ExposureVersion, version_id)
    now = _now()
    if type(version_id) is not int or row is None or row.user_id != user_id or row.created_at > now:
        raise ValueError('selected version is not available to this owner')
    data = {'user_id': user_id, 'version_id': version_id, 'selected_at': now.isoformat(), 'version_digest': row.digest}
    if dry_run:
        return _digest(data)
    selection = ExposureSelection(user_id=user_id, version_id=version_id, created_at=now, digest=_digest(data))
    existing = session.scalar(select(ExposureSelection).where(ExposureSelection.digest == selection.digest))
    if existing is not None:
        return existing.id
    session.add(selection)
    session.flush()
    return selection.id


def freeze_selected_exposure(session, *, user_id, cutoff_at, weights):
    """Creation boundary only. A replay uses its captured return value verbatim."""
    from app.selection_models import ExposureVersion, ExposureSelection
    if type(user_id) is not int or user_id <= 0:
        raise ValueError('invalid exposure owner')
    cutoff = _time(cutoff_at)
    exposure_totals({key: value for key, value in weights.items() if value is not None}, {})
    selected = session.scalar(select(ExposureSelection).where(ExposureSelection.user_id == user_id,
        ExposureSelection.created_at <= cutoff).order_by(ExposureSelection.id.desc()))
    result = {'version': VERSION, 'rules': RULES, 'user_id': user_id, 'cutoff_at': cutoff.isoformat(),
              'selection_id': None, 'version_id': None, 'version_digest': None,
              'policy': _policy_json(ExposurePolicy({}, None)), 'labels': {}, 'mappings': {},
              'weights': {key: None if value is None else _text_decimal(value) for key, value in sorted(weights.items())},
              'comparison': {'status': 'missing', 'reason': 'no_same_day_comparison'}, 'sources': {}}
    if selected is not None:
        row = session.get(ExposureVersion, selected.version_id)
        version = json.loads(row.payload_json)
        if (row.user_id != user_id or row.created_at > cutoff or row.digest != _digest(version)
                or version['rules'] != RULES or version['user_id'] != user_id):
            raise ValueError('exposure version integrity/cutoff mismatch')
        data = version['payload']
        result.update(selection_id=selected.id, version_id=row.id, version_digest=row.digest,
                      policy=data['policy'], selected_at=selected.created_at.isoformat(),
                      version_created_at=row.created_at.isoformat(), review=data['review'])
        cache = {}
        for code, mapping in data['mappings'].items():
            cache[code] = _sources(session, code, cutoff)
            state, reason = 'verified', None
            if _time(mapping['effective_at']) > cutoff:
                state, reason = 'missing', 'mapping_not_effective'
            source_states = {key: cache[code][key]['source_proof']['status'] if key in cache[code] else 'missing'
                             for key in mapping['evidence_ids']}
            if state == 'verified' and any(value != 'verified' for value in source_states.values()):
                state = next(value for value in ('untrusted', 'missing', 'not_applicable') if value in source_states.values())
                reason = 'source_unavailable_or_superseded'
            elif state == 'verified' and any(cache[code][key] != version['sources'][key] for key in mapping['evidence_ids']):
                state, reason = 'untrusted', 'source_changed'
            result['mappings'][code] = {**mapping, 'status': state, 'reason': reason, 'source_states': source_states}
            result['sources'].update({key: cache[code][key] for key in mapping['evidence_ids'] if key in cache[code]})
            if state == 'verified':
                result['labels'][code] = mapping['labels']
        comparison = data['comparison']
        if comparison is not None:
            code = comparison['code']
            state, reason = 'available', None
            if comparison['as_of_date'] != cutoff.astimezone(CHINA_MARKET_TIMEZONE).date().isoformat():
                state, reason = 'missing', 'comparison_date_mismatch'
            elif code not in result['labels'] or any(key not in cache[code]
                    or cache[code][key] != version['sources'][key]
                    or cache[code][key]['source_proof']['status'] != 'verified' for key in comparison['evidence_ids']):
                state, reason = 'untrusted', 'comparison_source_unavailable'
            result['comparison'] = {'status': state, 'reason': reason,
                'data': {**comparison, 'common_risks': result['labels'].get(code, [])} if state == 'available' else None}
            result['sources'].update({key: cache[code][key] for key in comparison['evidence_ids'] if key in cache[code]})
    return {**result, 'fingerprint': _digest(result)}


def read_exposure_context(context):
    if (not isinstance(context, dict) or context.get('version') != VERSION or context.get('rules') != RULES
            or context.get('fingerprint') != _digest({key: value for key, value in context.items() if key != 'fingerprint'})):
        raise ValueError('frozen exposure context integrity mismatch')
    weights = {key: _number(value) for key, value in context['weights'].items() if value is not None}
    labels = {key: tuple(value) for key, value in context['labels'].items()}
    exposure_totals(weights, labels)
    return weights, labels, _policy(context['policy'])


def exposure_report(context, delta_weights=None):
    weights, labels, policy = read_exposure_context(context)
    result = check_proposed_exposure(weights, delta_weights or {}, labels, policy)
    missing = [key for key, value in context['weights'].items() if value is None]
    return {'version_id': context['version_id'], 'selection_id': context['selection_id'],
            'fingerprint': context['fingerprint'], 'label_names': {**LABELS, 'unknown': '未知暴露'},
            'policy': context['policy'], 'configured': bool(policy.limits) or policy.unknown_limit is not None,
            'complete': not missing, 'unpriced_codes': missing,
            'allowed': result['allowed'] and not (missing and any(value > 0 for value in (delta_weights or {}).values())),
            'mapping_status': {key: {'status': value['status'], 'reason': value['reason'], 'source_states': value['source_states']}
                for key, value in context['mappings'].items()},
            **{name: {key: _text_decimal(value) for key, value in result[name].items()}
               for name in ('current_totals', 'proposed_totals', 'breaches')}}


def exact_book_weights(book):
    """The original Decimal book is the only weight boundary, never its float JSON."""
    result = {}
    with localcontext() as context:
        context.prec = 12300
        for row in book.positions:
            key = row.code or f'name:{row.name}'
            if row.weight is None or key in result and result[key] is None:
                result[key] = None
            else:
                result[key] = result.get(key, ZERO) + _decimal(row.weight)
    return result


def capture_review_input(session, *, review_id, user_id, payload):
    from app.models import PortfolioReview
    from app.selection_models import ReviewExposureCapture
    review = session.get(PortfolioReview, review_id)
    if review is None or review.user_id != user_id:
        raise ValueError('review capture owner mismatch')
    existing = session.scalar(select(ReviewExposureCapture).where(ReviewExposureCapture.review_id == review_id))
    if existing is not None:
        return read_review_capture(existing, user_id=user_id)
    read_exposure_context(payload['exposure_context'])
    if payload['exposure_context']['user_id'] != user_id:
        raise ValueError('exposure capture owner mismatch')
    data = {**payload, 'user_id': user_id, 'review_id': review_id}
    row = ReviewExposureCapture(review_id=review_id, user_id=user_id, created_at=_now(),
                                payload_json=_json(data), digest=_digest(data))
    session.add(row)
    session.flush()
    # First attempt and retry consume the identical canonical representation,
    # including nested object order when constructing model prompts.
    return read_review_capture(row, user_id=user_id)


def read_review_capture(row, *, user_id):
    data = json.loads(row.payload_json)
    if (row.user_id != user_id or data['user_id'] != user_id or data['review_id'] != row.review_id
            or _digest(data) != row.digest):
        raise ValueError('review capture identity mismatch')
    read_exposure_context(data['exposure_context'])
    if data['exposure_context']['user_id'] != user_id:
        raise ValueError('exposure capture owner mismatch')
    return data
