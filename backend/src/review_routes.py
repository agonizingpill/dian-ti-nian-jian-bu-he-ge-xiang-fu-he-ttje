"""双通道分流复核的送审、工作台与内部回调接口。"""

import os

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from . import review_sessions as service
from .auth import current_user, require_roles
from .services.queries import fetch_items, fetch_lift

router = APIRouter(prefix="/api/review-sessions", tags=["双通道分流复核"])
internal_router = APIRouter(prefix="/api/internal", tags=["内部回调"])

INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "jc1066-internal")


class SubmitIn(BaseModel):
    lift_id: int
    idempotency_key: str = Field(min_length=8, max_length=80)


class ConcludeIn(BaseModel):
    conclusion: str
    note: str = Field(min_length=4, max_length=500)


class ChannelResultIn(BaseModel):
    channel: str
    suggestion: str = Field(min_length=1, max_length=40)
    branch_signature: str = Field(min_length=1)
    model_version: str = Field(min_length=1, max_length=80)
    run_token: str = Field(min_length=8, max_length=40)


@router.post("", status_code=201)
def submit(payload: SubmitIn, user=Depends(require_roles("operator", "supervisor"))):
    lift = fetch_lift(payload.lift_id)
    if not lift:
        raise HTTPException(404, "电梯档案不存在")
    items = fetch_items(payload.lift_id)
    if len(items) < 3:
        raise HTTPException(409, "至少录入 3 个检验项后才能送审")
    view, replayed = service.submit(lift, items, payload.idempotency_key.strip(), user)
    view["replayed"] = replayed
    return view


@router.get("")
def list_sessions(user=Depends(current_user)):
    return {
        "sessions": service.list_sessions(),
        "timeout_seconds": service.REVIEW_TIMEOUT_SECONDS,
        "role": user["role"],
    }


@router.get("/{session_id}")
def session_detail(session_id: int, user=Depends(current_user)):
    service.sweep_expired()
    return service.session_view(session_id)


@router.post("/{session_id}/claim")
def claim(session_id: int, user=Depends(require_roles("supervisor"))):
    service.sweep_expired()
    return service.claim(session_id, user)


@router.post("/{session_id}/conclude")
def conclude(session_id: int, payload: ConcludeIn, user=Depends(require_roles("supervisor"))):
    return service.conclude_manual(session_id, user, payload.conclusion, payload.note)


def require_internal_token(x_internal_token: str = Header(default="")):
    if x_internal_token != INTERNAL_TOKEN:
        raise HTTPException(403, "内部回调令牌无效")


@internal_router.post("/review-sessions/{session_id}/channel-result")
def channel_result(session_id: int, payload: ChannelResultIn, _=Depends(require_internal_token)):
    return service.record_channel_result(
        session_id, payload.channel, payload.suggestion, payload.branch_signature,
        payload.model_version, payload.run_token)
