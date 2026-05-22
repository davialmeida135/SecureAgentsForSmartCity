import os
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..infra.audit import record_event
from ..infra.logging_utils import configure_logger
from ..infra.metrics import ERRORS_TOTAL, stage_timer

app = FastAPI(title="Citizen-Facing Interface")
logger = configure_logger("citizen_interface")

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8030/orchestrate")
HUMAN_APPROVAL_TOKEN = os.getenv("HUMAN_APPROVAL_TOKEN", "human-approval-token")
CITIZEN_AUTO_APPROVE = os.getenv("CITIZEN_AUTO_APPROVE", "true").lower() == "true"


class UserChatRequest(BaseModel):
    message: str
    token: Optional[str] = None
    location: Optional[str] = None


class ApprovalRequest(BaseModel):
    traceId: str
    plan: Dict[str, Any]
    token: Optional[str] = None


@app.post("/chat")
async def chat(request_body: UserChatRequest) -> Dict[str, Any]:
    payload = request_body.model_dump()
    with stage_timer("chat_request", "citizen_interface") as timing:
        try:
            response = requests.post(ORCHESTRATOR_URL, json=payload, timeout=8)
        except requests.RequestException as exc:
            ERRORS_TOTAL.labels(component="citizen_interface", kind="orchestrator_unavailable").inc()
            raise HTTPException(status_code=502, detail="Orchestrator unavailable") from exc

    if not response.ok:
        raise HTTPException(status_code=response.status_code, detail=response.text)

    logger.info(
        "User request handled",
        extra={
            "extra_fields": {
                "duration_ms": timing["duration_ms"],
            }
        },
    )
    return response.json()


@app.post("/approval")
async def approval(request_body: ApprovalRequest) -> Dict[str, Any]:
    trace_id = request_body.traceId
    approved = CITIZEN_AUTO_APPROVE
    payload = {
        "approved": approved,
        "human_token": HUMAN_APPROVAL_TOKEN if approved else None,
    }
    logger.info(
        "Plan approval evaluated",
        extra={
            "traceId": trace_id,
            "extra_fields": {"approved": approved},
        },
    )
    record_event(
        component="citizen_interface",
        event_type="PLAN_VALIDATED",
        trace_id=trace_id,
        actor="citizen_interface",
        outcome="approved" if approved else "rejected",
        payload={"approved": approved, "plan_id": request_body.plan.get("plan_id")},
    )
    return payload
