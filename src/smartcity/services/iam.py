import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from ..infra.audit import record_event
from ..infra.logging_utils import configure_logger
from ..infra.metrics import ERRORS_TOTAL, render_latest, stage_timer

app = FastAPI(title="Identity and Access Management")
logger = configure_logger("iam")

USER_TOKEN = os.getenv("USER_TOKEN", "user-token")
DEFAULT_PERMISSIONS = [
    value.strip()
    for value in os.getenv(
        "IAM_DEFAULT_PERMISSIONS",
        "traffic:read,traffic:write,traffic:notify,pump:read,pump:control",
    ).split(",")
    if value.strip()
]


class PermissionRequest(BaseModel):
    traceId: str
    token: Optional[str] = None


@app.post("/permissions")
async def permissions(request_body: PermissionRequest, request: Request) -> dict:
    trace_id = request_body.traceId
    token = request_body.token or request.headers.get("Authorization", "").replace(
        "Bearer ", ""
    )
    if token != USER_TOKEN:
        ERRORS_TOTAL.labels(component="iam", kind="unauthorized").inc()
        logger.warning("Unauthorized permissions request", extra={"traceId": trace_id})
        record_event(
            component="iam",
            event_type="PERMISSIONS_DENIED",
            trace_id=trace_id,
            actor="iam",
            outcome="unauthorized",
            payload={},
        )
        raise HTTPException(status_code=401, detail="Invalid token")

    with stage_timer("permissions", "iam") as timing:
        permissions_list: List[str] = list(DEFAULT_PERMISSIONS)

    logger.info(
        "Permissions issued",
        extra={
            "traceId": trace_id,
            "extra_fields": {
                "count": len(permissions_list),
                "duration_ms": timing["duration_ms"],
            },
        },
    )
    record_event(
        component="iam",
        event_type="PERMISSIONS_ISSUED",
        trace_id=trace_id,
        actor="iam",
        outcome="ok",
        payload={"permissions": permissions_list, "duration_ms": timing["duration_ms"]},
    )
    return {"permissions": permissions_list}


@app.get("/metrics")
def metrics() -> Response:
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)
