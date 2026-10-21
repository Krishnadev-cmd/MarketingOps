"""AI drafting, templates and the brand voice guide for the studio."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import runtime
from ..ai import prompts
from ..database import get_db
from ..enums import Role
from ..models import User
from ..schemas import DraftIn, FixIn
from ..security import current_user, require
from ..services import workflow

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.post("/draft")
def draft(data: DraftIn, db: Session = Depends(get_db), _: User = Depends(require(Role.CREATOR))):
    return runtime.drafter.draft(data.brief, data.channel, data.type, workflow.active_rules(db)).model_dump()


@router.post("/fix")
def fix(data: FixIn, db: Session = Depends(get_db), _: User = Depends(require(Role.CREATOR))):
    return runtime.drafter.fix(data.title, data.body, data.channel, data.type,
                               workflow.active_rules(db)).model_dump()


@router.get("/templates")
def templates(db: Session = Depends(get_db), _: User = Depends(current_user)):
    """Channel templates with today's required disclaimers already filled in."""
    rules = workflow.active_rules(db)
    out = []
    for t in prompts.TEMPLATES:
        needed = [r.text for r in rules if r.kind == "disclaimer" and r.severity == "block"
                  and r.applies_to(t["channel"])]
        out.append({**t, "body": t["body"].replace("{disclaimer}", "\n".join(needed))})
    return out


@router.get("/guides")
def guides(_: User = Depends(current_user)):
    return {"voice": prompts.BRAND_VOICE,
            "channels": [{"channel": c, "type": t, "guide": g} for (c, t), g in prompts.CHANNEL_GUIDES.items()]}
