"""Frozen research provenance, exact citations and deterministic coverage grades.

This validates source identity and structured facts, not the truth of qualitative
inference. Search results and model confidence never establish source trust.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re

from discovery.evidence_store import available_at, select_evidence_as_of
from discovery.field_dictionary import FIELD_DICTIONARY, SELECTION_FIELD_DICTIONARY
from discovery.fundamental_types import EvidenceRecord, evidence_identity
from discovery.fundamentals import _facts_calculation, evidence_from_payload, plain, period_order


CONTRACT_VERSION = 'research-evidence-v1'
FINANCIAL_POLICY = {'version': 'research-minimum-v1', 'required_fields': ['roe', 'net_income'],
                    'period': 'latest_frozen_financial_period', 'basis': 'comparable',
                    'source_proof': 'verified_retained_excerpt'}
_NUMBER = re.compile(r'(?<![\d.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')
_ASSERTION = re.compile(r'([a-z][a-z_0-9]*)\[([^\]\n]+)\]=([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)')
_FIELD_LABELS = {
    'revenue': '营业收入', 'net_income': '归母净利润', 'deducted_net_income': '扣非净利润',
    'roe': '净资产收益率', 'ocf': '经营现金流', 'capex': '资本开支', 'shares': '股数',
    'pretax_income': '税前利润', 'income_tax': '所得税', 'total_assets': '总资产',
    'net_assets': '净资产', 'cash': '现金', 'short_loan': '短期借款', 'long_loan': '长期借款',
    'bond_payable': '应付债券', 'noncurrent_liab_1year': '一年内到期非流动负债',
    'interest_debt': '有息负债', 'gross_margin': '毛利率', 'net_margin': '净利率',
    'interest_coverage': '利息覆盖倍数', 'ebit': '息税前利润', 'roic': '投入资本回报率',
    'normalized_effective_tax_rate': '正常化有效税率', 'invested_capital': '投入资本',
    'capital_adequacy': '资本充足率', 'core_tier1_capital_adequacy': '核心一级资本充足率',
    'npl': '不良贷款率', 'nonperforming_loan_balance': '不良贷款余额', 'impairment_loss': '减值损失',
    'government_grants': '政府补助', 'other_income': '其他收益', 'asset_disposal_gain': '资产处置收益',
    'goodwill': '商誉', 'acquisition_cash_paid': '收购支付现金', 'accounts_receivable': '应收账款',
    'contract_assets': '合同资产', 'inventory': '存货', 'total_equity_including_minority': '所有者权益',
    'interest_expense': '利息费用', 'restricted_cash': '受限资金', 'related_party_funds': '关联方资金',
    'amount': '金额', 'current_mcap': '当前市值', 'eps': '每股收益', 'bps': '每股净资产'}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value: object) -> str:
    return sha256(_json(value).encode('utf-8')).hexdigest()


def _time(value: object) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def research_grade(*, financial_complete: bool, valuation_complete: bool,
                   claim_coverage: Sequence[bool]) -> str:
    if not financial_complete or not valuation_complete:
        return 'C'
    return 'A' if len(claim_coverage) == 6 and all(claim_coverage) else 'B'


def _source_proof(row: EvidenceRecord, cutoff: datetime) -> dict[str, object]:
    """Verify retained bytes once, at freeze; readers never reopen the source."""
    excerpt = row.values.get('_source_excerpt')
    result = {'status': 'missing', 'reason': 'source_excerpt_unavailable',
        'method': None, 'document_sha256': row.document_sha256,
        'excerpt_sha256': None, 'excerpt': excerpt,
        'report_location': row.values.get('_report_location'), 'reviewed_at': None}
    path = row.values.get('_source_document_path')
    receipt_text = row.values.get('_excerpt_verification')
    if not all(_text(value) for value in (excerpt, path, receipt_text)):
        return result
    try:
        receipt = json.loads(receipt_text)
        reviewed = _time(receipt.get('reviewed_at'))
        source_review = json.loads(row.values.get('_verification') or '{}')
        source_reviewed = _time(source_review.get('reviewed_at'))
        digest = sha256(excerpt.encode('utf-8')).hexdigest()
        if (receipt.get('method') not in {'literal_utf8', 'human_review'}
                or receipt.get('document_sha256') != row.document_sha256
                or receipt.get('excerpt_sha256') != digest
                or receipt.get('report_location') != row.values.get('_report_location')
                or not _text(receipt.get('reviewer')) or reviewed is None
                or source_review.get('method') != 'human_review'
                or not _text(source_review.get('reviewer')) or source_reviewed is None
                or max(reviewed, source_reviewed) > min(cutoff, row.observed_at)):
            raise ValueError('source_excerpt_receipt_invalid')
        result.update(method=receipt['method'], excerpt_sha256=digest,
                      reviewed_at=reviewed.isoformat())
        if not Path(path).is_absolute():
            raise ValueError('source_document_path_invalid')
        raw = Path(path).read_bytes()
        if sha256(raw).hexdigest() != row.document_sha256:
            raise ValueError('source_hash_mismatch')
        if receipt['method'] == 'literal_utf8' and excerpt not in raw.decode('utf-8'):
            raise ValueError('source_excerpt_mismatch')
    except OSError:
        result.update(reason='source_document_unavailable')
    except (ValueError, TypeError, AttributeError, UnicodeError) as error:
        reason = str(error)
        result.update(status='untrusted', reason=reason if reason.startswith('source_') else 'source_excerpt_receipt_invalid')
    else:
        result.update(status='verified' if row.status == 'verified' else row.status,
                      reason=None if row.status == 'verified' else 'source_evidence_unverified')
    return result


def financial_complete(records: Sequence[EvidenceRecord], sources: Mapping[str, object]) -> bool:
    """Existing research minimum, checked on the latest frozen comparable facts."""
    rows = tuple(row for row in records if row.kind == 'financial')
    if not rows:
        return False
    latest = max((row.period for row in rows), key=period_order)
    result = _facts_calculation(rows, (('roe', latest), ('net_income', latest)), lambda roe, income: income)
    return result.status == 'verified' and all(
        isinstance(sources.get(key), Mapping) and sources[key].get('status') == 'verified'
        for key in result.evidence_ids)


def freeze_research_context(records: Sequence[EvidenceRecord], assumption_id: int | None,
                            cutoff_at: datetime) -> Mapping[str, object]:
    cutoff = _time(cutoff_at)
    if cutoff is None:
        raise ValueError('research cutoff must be an aware datetime')
    if assumption_id is not None and (type(assumption_id) is not int or assumption_id <= 0):
        raise ValueError('assumption_id must be a positive integer or None')
    codes = {row.code for row in records}
    if len(codes) > 1:
        raise ValueError('research evidence must belong to one company')
    if any(evidence_identity(row) != row.evidence_id for row in records):
        raise ValueError('research evidence identity mismatch')
    # Coverage wrappers are historical proof, never active claim facts.
    active = tuple(row for row in records if row.kind != 'coverage_reference')
    selected = select_evidence_as_of(active, code=next(iter(codes), ''), cutoff=cutoff)
    sources = {row.evidence_id: _source_proof(row, cutoff) for row in selected}
    model_context = {'contract_version': CONTRACT_VERSION, 'cutoff_at': cutoff.isoformat(),
        'financial_policy': plain(FINANCIAL_POLICY), 'evidence': [
            {'evidence_id': row.evidence_id, 'code': row.code, 'kind': row.kind, 'period': row.period,
             'source_url': row.source_url, 'document_sha256': row.document_sha256,
             'status': row.status, 'basis_id': row.basis_id,
             'values': {key: value for key, value in row.values.items() if not key.startswith('_')},
             'source_proof': sources[row.evidence_id]}
            for row in selected]}
    context = {'contract_version': CONTRACT_VERSION, 'cutoff_at': cutoff.isoformat(),
        'financial_policy': plain(FINANCIAL_POLICY), 'model_context': model_context,
        'assumption_id': assumption_id, 'records': plain(selected),
        'evidence_ids': [row.evidence_id for row in selected], 'sources': sources,
        'financial_complete': financial_complete(selected, sources),
        # T12 must replace this with validated persisted valuation, not an ID check.
        'valuation_complete': False, 'valuation_reason': 'validated_valuation_unavailable'}
    return {**context, 'fingerprint': _digest(context)}


def read_research_context(context: Mapping[str, object]) -> tuple[EvidenceRecord, ...]:
    """Check the retained context without re-reading files or live source tables."""
    if (context.get('contract_version') != CONTRACT_VERSION
            or context.get('financial_policy') != FINANCIAL_POLICY
            or context.get('fingerprint') != _digest({key: value for key, value in context.items() if key != 'fingerprint'})):
        raise ValueError('frozen research context integrity mismatch')
    records = tuple(evidence_from_payload(row) for row in context['records'])
    cutoff = _time(context.get('cutoff_at'))
    if cutoff is None or len({row.code for row in records}) > 1:
        raise ValueError('frozen research context company/cutoff mismatch')
    selected = select_evidence_as_of(records, code=records[0].code if records else '', cutoff=cutoff)
    if (selected != records or any(row.kind == 'coverage_reference' for row in records)
            or context['evidence_ids'] != [row.evidence_id for row in records]
            or set(context['sources']) != {row.evidence_id for row in records}):
        raise ValueError('frozen research context evidence mismatch')
    return records


def _financial_errors(claim: Mapping[str, object], sources: Mapping[str, EvidenceRecord]) -> set[str]:
    errors, bound = set(), set()
    facts = claim.get('financial_facts', [])
    if not isinstance(facts, (list, tuple)):
        return {'financial_facts_invalid'}
    for fact in facts:
        if not isinstance(fact, Mapping):
            errors.add('financial_facts_invalid')
            continue
        source_id = fact.get('evidence_id')
        row = sources.get(source_id) if isinstance(source_id, str) else None
        field, value = fact.get('field'), fact.get('value')
        try:
            if (row is None or fact.get('evidence_id') not in claim.get('evidence_ids', ())
                    or not isinstance(field, str) or field.startswith('_')
                    or fact.get('period') != row.period or not isinstance(value, str)):
                raise ValueError
            actual, asserted = Decimal(row.values.get(field)), Decimal(value)
            if not actual.is_finite() or not asserted.is_finite() or actual != asserted:
                raise ValueError
            bound.add((field, row.period, asserted))
        except (InvalidOperation, TypeError, ValueError):
            errors.add('financial_value_mismatch')
    text = claim.get('claim', '')
    if isinstance(text, str):
        def check_span(match):
            if (match[1], match[2], Decimal(match[3])) not in bound:
                errors.add('financial_text_unbound')
            return ''
        remaining = _ASSERTION.sub(check_span, text)
        # Explicit list numbering is structural; all other numeric prose needs
        # a binding. This conservative protocol makes no semantic NLP claims.
        remaining = re.sub(r'(?m)^\s*\d+[.)、]\s+', '', remaining)
        if _NUMBER.search(remaining):
            errors.add('financial_text_unbound')
    return errors


def render_claim_text(claim: Mapping[str, object]) -> str:
    """Readable presentation; the audit retains the exact protocol/fact spans."""
    text = claim.get('claim')
    if not isinstance(text, str):
        return ''
    def render_span(match):
        field, period, amount = match.groups()
        label = _FIELD_LABELS.get(field, field.replace('_', ' '))
        if claim.get('evidence_status') != 'verified':
            return f'{label}（{period}，数值未核验）'
        spec = FIELD_DICTIONARY.get(field) or SELECTION_FIELD_DICTIONARY.get(field)
        unit = spec.unit if spec else {'amount': 'CNY', 'current_mcap': 'CNY',
            'eps': 'CNY/share', 'bps': 'CNY/share'}.get(field, '')
        suffix = {'CNY': ' 元', 'percent': '%', 'shares': ' 股', 'rate': ' 倍',
                  'ratio': '（比例）', 'CNY/share': ' 元/股'}.get(unit, '')
        return f'{label}（{period}）：{amount}{suffix}'
    rendered = _ASSERTION.sub(render_span, text)
    if {'financial_text_unbound', 'financial_value_mismatch'}.intersection(claim.get('errors', ())):
        return '未核验模型数值陈述：' + rendered
    return rendered


def validate_claims(claims: Sequence[Mapping[str, object]],
                    records: Sequence[EvidenceRecord]) -> tuple[str, ...]:
    sources = {row.evidence_id: row for row in records}
    result = []
    for index, claim in enumerate(claims):
        errors: set[str] = set()
        if not isinstance(claim, Mapping):
            result.append(f'claim[{index}]:claim_invalid')
            continue
        if not _text(claim.get('claim')):
            errors.add('claim_missing')
        if not _text(claim.get('falsification_condition')):
            errors.add('falsification_missing')
        checked = _time(claim.get('checked_at'))
        if checked is None:
            errors.add('checked_at_invalid')
        if not _text(claim.get('status')) or claim['status'] not in {'pass', 'fail', 'gray', 'supported', 'contradicted', 'incomplete'}:
            errors.add('status_invalid')
        elif claim['status'] in {'gray', 'incomplete'}:
            errors.add('claim_unresolved')
        ids, counter = claim.get('evidence_ids'), claim.get('counterevidence_ids')
        if not isinstance(ids, (list, tuple)) or not ids or not all(_text(key) for key in ids):
            errors.add('evidence_missing')
            ids = []
        if not isinstance(counter, (list, tuple)) or not all(_text(key) for key in counter):
            errors.add('counterevidence_invalid')
            counter = []
        if not counter:
            search = claim.get('counterevidence_search')
            if (not isinstance(search, Mapping) or search.get('result') != 'not_found'
                    or not _text(search.get('scope'))):
                errors.add('counterevidence_search_missing')
        cited = set(ids) | set(counter)
        for key in cited:
            row = sources.get(key)
            if row is None:
                errors.add('evidence_unknown')
                continue
            if evidence_identity(row) != key:
                errors.add('evidence_identity_mismatch')
            if row.kind == 'coverage_reference':
                errors.add('evidence_proof_only')
            if row.status != 'verified':
                errors.add('evidence_unverified')
            if checked is not None and available_at(row) > checked:
                errors.add('evidence_future')
        citations = claim.get('citations')
        quoted = set()
        if not isinstance(citations, (list, tuple)):
            citations = []
        for citation in citations:
            if not isinstance(citation, Mapping) or not isinstance(citation.get('evidence_id'), str):
                errors.add('citation_invalid')
                continue
            key = citation['evidence_id']
            row = sources.get(key)
            if key not in cited or row is None:
                errors.add('citation_unknown')
                continue
            quoted.add(key)
            if citation.get('document_sha256') != row.document_sha256:
                errors.add('citation_hash_mismatch')
            excerpt = row.values.get('_source_excerpt')
            if not _text(citation.get('excerpt')) or not excerpt or citation['excerpt'] not in excerpt:
                errors.add('citation_excerpt_mismatch')
        if cited - quoted:
            errors.add('citation_missing')
        errors.update(_financial_errors(claim, sources))
        result.extend(f'claim[{index}]:{reason}' for reason in sorted(errors))
    return tuple(result)
