import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from ..infra.audit import record_event
from ..infra.logging_utils import configure_logger
from ..infra.metrics import ERRORS_TOTAL, MCP_CALLS_TOTAL, render_latest, stage_timer
from ..infra.ngsi_client import get_entity

app = FastAPI(title="Context Broker MCP Server")
logger = configure_logger("context_broker_mcp_server")

USER_TOKEN = os.getenv("USER_TOKEN", "user-token")
WEATHER_STATION_ID = os.getenv("WEATHER_STATION_ID", "WeatherStation:001")


class McpCall(BaseModel):
    method: str
    params: Dict[str, Any]
    traceId: str
    token: Optional[str] = None


@app.post("/mcp")
async def handle_mcp(call: McpCall, request: Request):
    trace_id = call.traceId
    token = call.token or request.headers.get("Authorization", "").replace(
        "Bearer ", ""
    )
    if token != USER_TOKEN:
        MCP_CALLS_TOTAL.labels(method=call.method, status="401").inc()
        ERRORS_TOTAL.labels(component="context_broker_mcp_server", kind="unauthorized").inc()
        logger.warning("Unauthorized MCP call", extra={"traceId": trace_id})
        record_event(
            component="context_broker_mcp_server",
            event_type="MCP_UNAUTHORIZED",
            trace_id=trace_id,
            actor="context_broker_mcp_server",
            outcome="unauthorized",
            payload={"method": call.method, "params": call.params},
        )
        raise HTTPException(status_code=401, detail="Invalid token")

    status_label = "200"
    try:
        with stage_timer("mcp_call_server", "context_broker_mcp_server") as timing:
            if call.method in {"getWeatherStationData", "getContext"}:
                entity_id = call.params.get("entity_id", WEATHER_STATION_ID)
                result = get_entity(entity_id, trace_id, token)
            else:
                status_label = "400"
                raise HTTPException(status_code=400, detail="Unknown method")
    except HTTPException as http_exc:
        status_label = str(http_exc.status_code)
        MCP_CALLS_TOTAL.labels(method=call.method, status=status_label).inc()
        record_event(
            component="context_broker_mcp_server",
            event_type="MCP_CALL_REJECTED",
            trace_id=trace_id,
            actor="context_broker_mcp_server",
            outcome=status_label,
            payload={
                "method": call.method,
                "params": call.params,
                "detail": str(http_exc.detail),
            },
        )
        raise
    except Exception as exc:  # pragma: no cover
        status_label = "500"
        MCP_CALLS_TOTAL.labels(method=call.method, status=status_label).inc()
        ERRORS_TOTAL.labels(component="context_broker_mcp_server", kind="tool_error").inc()
        logger.exception("Context broker MCP error", extra={"traceId": trace_id})
        record_event(
            component="context_broker_mcp_server",
            event_type="MCP_CALL_ERROR",
            trace_id=trace_id,
            actor="context_broker_mcp_server",
            outcome="error",
            payload={
                "method": call.method,
                "params": call.params,
                "error": str(exc),
            },
        )
        raise HTTPException(status_code=500, detail=str(exc))

    MCP_CALLS_TOTAL.labels(method=call.method, status=status_label).inc()
    logger.info(
        "Context broker MCP call executed",
        extra={
            "traceId": trace_id,
            "extra_fields": {
                "method": call.method,
                "duration_ms": timing["duration_ms"],
            },
        },
    )
    record_event(
        component="context_broker_mcp_server",
        event_type="MCP_CALL",
        trace_id=trace_id,
        actor="context_broker_mcp_server",
        outcome="ok",
        payload={
            "method": call.method,
            "params": call.params,
            "duration_ms": timing["duration_ms"],
        },
    )
    return {"result": result}


@app.get("/metrics")
def metrics() -> Response:
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)
