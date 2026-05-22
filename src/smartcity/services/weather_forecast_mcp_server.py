import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from ..infra.audit import record_event
from ..infra.logging_utils import configure_logger
from ..infra.metrics import ERRORS_TOTAL, MCP_CALLS_TOTAL, render_latest, stage_timer

app = FastAPI(title="Weather Forecast MCP Server")
logger = configure_logger("weather_forecast_mcp_server")

USER_TOKEN = os.getenv("USER_TOKEN", "user-token")


class McpCall(BaseModel):
    method: str
    params: Dict[str, Any]
    traceId: str
    token: Optional[str] = None


def _build_forecast(params: Dict[str, Any]) -> Dict[str, Any]:
    station = params.get("weather_station_data") or {}
    station_weather = str(station.get("weather", "")).lower()
    station_status = str(station.get("status", "")).lower()
    station_notes = str(station.get("notes", "")).lower()
    station_flood = bool(station.get("floodRisk", False))

    summary = "clear"
    rain_probability = 0.1
    if station_flood or "flood" in station_weather or "flood" in station_status:
        summary = "flood-risk"
        rain_probability = 0.9
    elif any(term in station_weather for term in ("rain", "storm")):
        summary = "heavy-rain"
        rain_probability = 0.8
    elif "rain" in station_status or "rain" in station_notes:
        summary = "rain"
        rain_probability = 0.6

    return {
        "summary": summary,
        "rain_probability": rain_probability,
        "source": "stub",
        "location": params.get("location"),
    }


@app.post("/mcp")
async def handle_mcp(call: McpCall, request: Request):
    trace_id = call.traceId
    token = call.token or request.headers.get("Authorization", "").replace(
        "Bearer ", ""
    )
    if token != USER_TOKEN:
        MCP_CALLS_TOTAL.labels(method=call.method, status="401").inc()
        ERRORS_TOTAL.labels(component="weather_forecast_mcp_server", kind="unauthorized").inc()
        logger.warning("Unauthorized MCP call", extra={"traceId": trace_id})
        record_event(
            component="weather_forecast_mcp_server",
            event_type="MCP_UNAUTHORIZED",
            trace_id=trace_id,
            actor="weather_forecast_mcp_server",
            outcome="unauthorized",
            payload={"method": call.method, "params": call.params},
        )
        raise HTTPException(status_code=401, detail="Invalid token")

    status_label = "200"
    try:
        with stage_timer("mcp_call_server", "weather_forecast_mcp_server") as timing:
            if call.method == "getWeatherForecast":
                result = _build_forecast(call.params)
            else:
                status_label = "400"
                raise HTTPException(status_code=400, detail="Unknown method")
    except HTTPException as http_exc:
        status_label = str(http_exc.status_code)
        MCP_CALLS_TOTAL.labels(method=call.method, status=status_label).inc()
        record_event(
            component="weather_forecast_mcp_server",
            event_type="MCP_CALL_REJECTED",
            trace_id=trace_id,
            actor="weather_forecast_mcp_server",
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
        ERRORS_TOTAL.labels(component="weather_forecast_mcp_server", kind="tool_error").inc()
        logger.exception("Weather MCP error", extra={"traceId": trace_id})
        record_event(
            component="weather_forecast_mcp_server",
            event_type="MCP_CALL_ERROR",
            trace_id=trace_id,
            actor="weather_forecast_mcp_server",
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
        "Weather forecast MCP call executed",
        extra={
            "traceId": trace_id,
            "extra_fields": {
                "method": call.method,
                "duration_ms": timing["duration_ms"],
            },
        },
    )
    record_event(
        component="weather_forecast_mcp_server",
        event_type="MCP_CALL",
        trace_id=trace_id,
        actor="weather_forecast_mcp_server",
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
