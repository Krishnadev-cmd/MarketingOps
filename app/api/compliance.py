"""The checker as an endpoint (the editor calls it as you type), and rule management."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..enums import Role
from ..models import ComplianceRule, User
from ..schemas import RuleIn, ScanIn, rule_out
from ..security import current_user, require
from ..services import compliance, workflow
from ..services.audit import record

router = APIRouter(prefix="/api/compliance", tags=["compliance"])

RULE_EDITORS = (Role.COMPLIANCE, Role.ADMIN)


@router.post("/scan")
def scan(data: ScanIn, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return compliance.scan(data.title, data.body, data.channel, data.type, workflow.active_rules(db))


@router.get("/rules")
def list_rules(db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = db.scalars(select(ComplianceRule).order_by(ComplianceRule.kind.desc(), ComplianceRule.id)).all()
    return [rule_out(r) for r in rows]


@router.post("/rules", status_code=201)
def create_rule(data: RuleIn, db: Session = Depends(get_db), user: User = Depends(require(*RULE_EDITORS))):
    _validate(data)
    rule = ComplianceRule(**data.model_dump())
    db.add(rule)
    db.flush()
    record(db, user, "rule.created", "rule", rule.id, after=rule_out(rule))
    db.commit()
    return rule_out(rule)


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, data: RuleIn, db: Session = Depends(get_db),
                user: User = Depends(require(*RULE_EDITORS))):
    rule = db.get(ComplianceRule, rule_id)
    if rule is None:
        raise HTTPException(404, "No such rule.")
    _validate(data)
    before = rule_out(rule)
    for key, value in data.model_dump().items():
        setattr(rule, key, value)
    record(db, user, "rule.updated", "rule", rule.id, before=before, after=rule_out(rule))
    db.commit()
    return rule_out(rule)


def _validate(data: RuleIn) -> None:
    """Rules are never deleted (auditors want history); deactivate them instead."""
    if data.kind == "prohibited":
        try:
            compliance.phrase_pattern(data.text)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
