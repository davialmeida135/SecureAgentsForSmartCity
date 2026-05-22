import os
import uuid
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..core.executor import execute_candidate_plan
from ..core.models import ActionType, ApprovalMode, MonitorEvent
from ..core.planner import build_candidate_plan
from ..core.policy_engine import evaluate_plan
from ..infra.audit import record_event
from ..infra.logging_utils import configure_logger
from ..infra.metrics import ERRORS_TOTAL, stage_timer

app = FastAPI(title="LLM Service Orchestrator")
logger = configure_logger("orchestrator")

IAM_URL = os.getenv("IAM_URL", "http://localhost:8020")
CITIZEN_INTERFACE_URL = os.getenv(
    "CITIZEN_INTERFACE_URL", "http://localhost:8040"
)
CONTEXT_BROKER_MCP_URL = os.getenv(
    "CONTEXT_BROKER_MCP_URL", "http://localhost:8001/mcp"
)
WEATHER_FORECAST_MCP_URL = os.getenv(
    "WEATHER_FORECAST_MCP_URL", "http://localhost:8003/mcp"
)
WEATHER_STATION_ID = os.getenv("WEATHER_STATION_ID", "WeatherStation:001")
USER_TOKEN = os.getenv("USER_TOKEN", "user-token")

IAM_TIMEOUT_SECONDS = float(os.getenv("IAM_TIMEOUT_SECONDS", "3.0"))
MCP_TIMEOUT_SECONDS = float(os.getenv("MCP_TIMEOUT_SECONDS", "4.0"))
CITIZEN_TIMEOUT_SECONDS = float(os.getenv("CITIZEN_TIMEOUT_SECONDS", "4.0"))

ACTION_PERMISSIONS = {
    ActionType.NOTIFY_TRAFFIC_AGENTS: "traffic:notify",
    ActionType.GET_PUMP_STATUS: "pump:read",
    ActionType.ACTIVATE_PUMP: "pump:control",
    ActionType.DEACTIVATE_PUMP: "pump:control",
}


class OrchestratorRequest(BaseModel):
    message: str
    token: Optional[str] = None
    location: Optional[str] = None


def _post_json(
    url: str, payload: Dict[str, Any], *, timeout: float, trace_id: str, kind: str
) -> Dict[str, Any]:
    try:
        response = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        ERRORS_TOTAL.labels(component="orchestrator", kind=kind).inc()
        raise HTTPException(status_code=502, detail=f"{kind} unavailable") from exc
    if not response.ok:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


def _fetch_permissions(token: str, trace_id: str) -> List[str]:
    payload = {"traceId": trace_id, "token": token}
    response = _post_json(
        f"{IAM_URL.rstrip('/')}/permissions",
        payload,
        timeout=IAM_TIMEOUT_SECONDS,
        trace_id=trace_id,
        kind="iam",
    )
    permissions = response.get("permissions")
    if not isinstance(permissions, list):
        raise HTTPException(status_code=502, detail="IAM response malformed")
    return [str(value) for value in permissions]


def _fetch_context(token: str, trace_id: str) -> Dict[str, Any]:
    payload = {
        "method": "getWeatherStationData",
        "params": {"entity_id": WEATHER_STATION_ID},
        "traceId": trace_id,
        "token": token,
    }
    response = _post_json(
        CONTEXT_BROKER_MCP_URL,
        payload,
        timeout=MCP_TIMEOUT_SECONDS,
        trace_id=trace_id,
        kind="context_broker",
    )
    result = response.get("result")
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Context response malformed")
    return result


def _fetch_forecast(
    token: str, trace_id: str, location: Optional[str], context: Dict[str, Any]
) -> Dict[str, Any]:
    payload = {
        "method": "getWeatherForecast",
        "params": {"location": location, "weather_station_data": context},
        "traceId": trace_id,
        "token": token,
    }
    response = _post_json(
        WEATHER_FORECAST_MCP_URL,
        payload,
        timeout=MCP_TIMEOUT_SECONDS,
        trace_id=trace_id,
        kind="weather_forecast",
    )
    result = response.get("result")
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Forecast response malformed")
    return result


def _request_user_approval(
    token: str, trace_id: str, plan: Dict[str, Any]
) -> Dict[str, Any]:
    payload = {"traceId": trace_id, "token": token, "plan": plan}
    response = _post_json(
        f"{CITIZEN_INTERFACE_URL.rstrip('/')}/approval",
        payload,
        timeout=CITIZEN_TIMEOUT_SECONDS,
        trace_id=trace_id,
        kind="citizen_interface",
    )
    return response


def _flag_from_text(text: str, keywords: List[str]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def _build_event(
    message: str,
    location: Optional[str],
    weather_station: Dict[str, Any],
    forecast: Dict[str, Any],
    permissions: List[str],
) -> MonitorEvent:
    ambulance_detected = _flag_from_text(message, ["ambulance", "ambulancia"])
    heavy_rain = _flag_from_text(message, ["heavy rain", "storm", "rain", "chuva"])
    flood_risk = _flag_from_text(message, ["flood", "inundacao", "flood-risk"])

    station_weather = str(weather_station.get("weather", "")).lower()
    station_status = str(weather_station.get("status", "")).lower()
    if any(term in station_weather for term in ("rain", "storm")):
        heavy_rain = True
    if "flood" in station_weather or "flood" in station_status:
        flood_risk = True
    if bool(weather_station.get("floodRisk", False)):
        flood_risk = True

    forecast_summary = str(forecast.get("summary", "")).lower()
    if "rain" in forecast_summary or "storm" in forecast_summary:
        heavy_rain = True
    if "flood" in forecast_summary:
        flood_risk = True

    if ambulance_detected and (heavy_rain or flood_risk):
        event_type = "combined-flood-corridor"
    elif ambulance_detected:
        event_type = "ambulance-only"
    elif heavy_rain or flood_risk:
        event_type = "flood-response"
    else:
        event_type = "baseline"

    resolved_location = (
        location
        or str(weather_station.get("location", "")).strip()
        or "unknown"
    )

    return MonitorEvent(
        event_type=event_type,
        ambulance_detected=ambulance_detected,
        heavy_rain=heavy_rain,
        flood_risk=flood_risk,
        crowd_level="normal",
        location=resolved_location,
        notes=f"user_input: {message}",
        weather_station_data=weather_station,
        weather_forecast=forecast,
        user_permissions=permissions,
    )


def _missing_permissions(plan: Dict[str, Any], permissions: List[str]) -> List[str]:
    missing = set()
    for step in plan.get("steps", []):
        action = step.get("action")
        if not action:
            continue
        try:
            action_type = ActionType(action)
        except ValueError:
            continue
        required = ACTION_PERMISSIONS.get(action_type)
        if required and required not in permissions:
            missing.add(required)
    return sorted(missing)


@app.post("/orchestrate")
async def orchestrate(request_body: OrchestratorRequest) -> Dict[str, Any]:
    trace_id = str(uuid.uuid4())
    token = request_body.token or USER_TOKEN
    with stage_timer("orchestrate", "orchestrator") as timing:
        record_event(
            component="orchestrator",
            event_type="USER_INPUT_RECEIVED",
            trace_id=trace_id,
            actor="citizen_interface",
            outcome="received",
            payload={"message": request_body.message},
        )

        permissions = _fetch_permissions(token, trace_id)
        record_event(
            component="orchestrator",
            event_type="PERMISSIONS_RECEIVED",
            trace_id=trace_id,
            actor="iam",
            outcome="ok",
            payload={"permissions": permissions},
        )

        context = _fetch_context(token, trace_id)
        record_event(
            component="orchestrator",
            event_type="CONTEXT_FETCHED",
            trace_id=trace_id,
            actor="context_broker_mcp_server",
            outcome="ok",
            payload={"context_keys": list(context.keys())},
        )

        forecast = _fetch_forecast(
            token, trace_id, request_body.location, context
        )
        record_event(
            component="orchestrator",
            event_type="FORECAST_FETCHED",
            trace_id=trace_id,
            actor="weather_forecast_mcp_server",
            outcome="ok",
            payload={"summary": forecast.get("summary")},
        )

        event = _build_event(
            request_body.message, request_body.location, context, forecast, permissions
        )
        plan = build_candidate_plan(event, trace_id)

        missing_permissions = _missing_permissions(plan.to_wire_dict(), permissions)
        if missing_permissions:
            record_event(
                component="orchestrator",
                event_type="PERMISSIONS_MISSING",
                trace_id=trace_id,
                plan_id=plan.plan_id,
                actor="orchestrator",
                outcome="blocked",
                payload={"missing": missing_permissions},
            )
            raise HTTPException(
                status_code=403,
                detail=f"Missing permissions: {', '.join(missing_permissions)}",
            )

        decision = evaluate_plan(
            plan=plan.to_wire_dict(), provided_token=token, trace_id=trace_id
        )

        if decision.approval_mode == ApprovalMode.HUMAN and not decision.allowed:
            approval_response = _request_user_approval(
                token, trace_id, plan.to_wire_dict()
            )
            approved = bool(approval_response.get("approved", False))
            human_token = approval_response.get("human_token")
            record_event(
                component="orchestrator",
                event_type="USER_VALIDATION",
                trace_id=trace_id,
                plan_id=plan.plan_id,
                actor="citizen_interface",
                outcome="approved" if approved else "rejected",
                payload={"approved": approved},
            )
            if approved and human_token:
                plan.approval.human_token = human_token
                decision = evaluate_plan(
                    plan=plan.to_wire_dict(),
                    provided_token=token,
                    trace_id=trace_id,
                )

        if not decision.allowed:
            record_event(
                component="orchestrator",
                event_type="PLAN_BLOCKED",
                trace_id=trace_id,
                plan_id=plan.plan_id,
                actor="policy_engine",
                outcome=decision.approval_mode.value,
                payload={"reason": decision.reason},
            )
            return {
                "traceId": trace_id,
                "plan": plan.to_wire_dict(),
                "policy": decision.model_dump(),
                "executed": False,
                "context": context,
                "forecast": forecast,
                "duration_ms": timing["duration_ms"],
            }

        report = execute_candidate_plan(
            plan, provided_token=token, policy_decision=decision
        )
        record_event(
            component="orchestrator",
            event_type="PLAN_EXECUTED",
            trace_id=trace_id,
            plan_id=plan.plan_id,
            actor="executor",
            outcome="executed",
            payload={"steps": len(report.step_results)},
        )

    return {
        "traceId": trace_id,
        "plan": plan.to_wire_dict(),
        "policy": decision.model_dump(),
        "executed": report.executed,
        "step_results": [result.model_dump() for result in report.step_results],
        "context": context,
        "forecast": forecast,
        "duration_ms": timing["duration_ms"],
    }
