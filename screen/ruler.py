from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RulerVersion, Setting
from ledger.positions import LOCAL_USER_ID
from screen import funnel

GATE_ACK_KEY = "ruler_gate_ack"

# ponytail: eval shape copied so screen does not import research; change evaluate.py and here together.

INITIAL_REASON = "初始尺子"


class RulerError(ValueError):
    def __init__(self, error: str, code: str, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.code = code
        self.detail = detail


def current_payload() -> dict[str, object]:
    return {
        "eval": {
            "bands": sorted(("aggressive", "steady", "conservative", "watch")),
            "gates": ["能力圈", "好生意", "护城河", "管理层", "安全边际", "决策纪律"],
            "memo_roles": sorted(("core", "satellite", "skip")),
            "mirror_min": 5,
            "required": [
                "verdict",
                "gates",
                "mirror_test",
                "price_bands",
                "memo",
                "data_grade",
                "as_of",
                "portfolio_fit",
            ],
            "verdicts": sorted(("pass", "fail", "gray")),
        },
        "funnel": {
            "ELASTIC_INDUSTRIES": list(funnel.ELASTIC_INDUSTRIES),
            "EXEMPT_C_OCF_NI": str(funnel.EXEMPT_C_OCF_NI),
            "EXEMPT_C_ROE": str(funnel.EXEMPT_C_ROE),
            "EXEMPT_GROSS_MARGIN": str(funnel.EXEMPT_GROSS_MARGIN),
            "FINALIST_LIMIT": funnel.FINALIST_LIMIT,
            "GROSS_MARGIN_FLOOR": str(funnel.GROSS_MARGIN_FLOOR),
            "HIGH_CERT_ROE": str(funnel.HIGH_CERT_ROE),
            "INTEREST_COVER_MIN": str(funnel.INTEREST_COVER_MIN),
            "KEEP_LIMIT": funnel.KEEP_LIMIT,
            "NET_MARGIN_FLOOR": str(funnel.NET_MARGIN_FLOOR),
            "OCF_NI_FLOOR": str(funnel.OCF_NI_FLOOR),
            "PEG_ELASTIC": str(funnel.PEG_ELASTIC),
            "ROE_FLOOR": str(funnel.ROE_FLOOR),
            "SHARE_GROWTH_MAX": str(funnel.SHARE_GROWTH_MAX),
            "THIN_MODELS": sorted(funnel.THIN_MODELS),
        },
    }


def current_hash() -> str:
    blob = json.dumps(current_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def latest(session: Session) -> RulerVersion | None:
    return session.scalars(
        select(RulerVersion)
        .where(RulerVersion.user_id == LOCAL_USER_ID)
        .order_by(RulerVersion.id.desc())
    ).first()


def activate(session: Session, reason: object) -> RulerVersion:
    text = str(reason or "").strip()
    if not text:
        raise RulerError("请求无效", "RULER_REASON", "改因不能为空")
    digest = current_hash()
    row = latest(session)
    if row is not None and row.digest == digest:
        _set_ack(session, row.id)
        return row
    version = RulerVersion(
        user_id=LOCAL_USER_ID,
        digest=digest,
        reason=text,
        payload_json=json.dumps(current_payload(), ensure_ascii=False, sort_keys=True),
        activated_at=datetime.now(timezone.utc),
    )
    session.add(version)
    session.flush()
    _set_ack(session, None)
    return version


def _set_ack(session: Session, version_id: int | None) -> None:
    if version_id is None:
        row = session.get(Setting, GATE_ACK_KEY)
        if row is not None:
            session.delete(row)
        return
    session.merge(Setting(key=GATE_ACK_KEY, value=str(version_id)))


def seed_if_empty(session: Session) -> None:
    if latest(session) is None:
        row = activate(session, INITIAL_REASON)
        _set_ack(session, row.id)
        session.commit()


def require_matched(session: Session) -> RulerVersion:
    row = latest(session)
    digest = current_hash()
    if row is None or row.digest != digest:
        raise RulerError(
            "尺子未生效",
            "RULER_MISMATCH",
            "当前尺子与已生效版本不一致，请写下改因后再开漏斗",
        )
    return row


def _row_payload(row: RulerVersion) -> dict[str, object]:
    return {
        "id": row.id,
        "digest": row.digest,
        "reason": row.reason,
        "activated_at": row.activated_at.isoformat(),
    }


def status(session: Session) -> dict[str, object]:
    digest = current_hash()
    rows = session.scalars(
        select(RulerVersion)
        .where(RulerVersion.user_id == LOCAL_USER_ID)
        .order_by(RulerVersion.id.desc())
    ).all()
    active = rows[0] if rows else None
    from screen.replay import replay_report

    body: dict[str, object] = {
        "digest": digest,
        "matched": active is not None and active.digest == digest,
        "active": None if active is None else _row_payload(active),
        "versions": [_row_payload(row) for row in rows],
    }
    body.update(replay_report(session))
    return body
