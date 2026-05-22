from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from ..infra.audit import record_event
from ..infra.logging_utils import configure_logger
from ..infra.metrics import ERRORS_TOTAL, PLANS_TOTAL, stage_timer
from .llm_planner import generate_plan_with_llm
from .models import (
    ActionType,
    CandidatePlan,
    MonitorEvent,
    RiskLevel,
    validate_plan_dict,
)

load_dotenv()

logger = configure_logger("planner")

PUMP_ID = os.getenv("PUMP_ID", "Pump:001")


def _risk_from_event(event: MonitorEvent) -> RiskLevel:
    if event.flood_risk:
        return RiskLevel.HIGH
    if event.heavy_rain or event.crowd_level.lower() in {"high", "dense"}:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _approval_level(risk_level: RiskLevel) -> int:
    if risk_level == RiskLevel.LOW:
        return 1
    if risk_level == RiskLevel.MEDIUM:
        return 2
    return 3


def _build_rule_based_plan(event: MonitorEvent, trace_id: str) -> Dict[str, Any]:
    risk_level = _risk_from_event(event)
    autonomy_level = _approval_level(risk_level)

    flood_event = event.heavy_rain or event.flood_risk

    if event.ambulance_detected and flood_event:
        scenario = "combined-flood-corridor"
        goal = "Coordinate emergency corridor with flood mitigation"
        message = "Emergency corridor and pump activation engaged"
        pump_mode = "high" if event.flood_risk else "auto"
        steps = [
            {
                "id": "activate-pump",
                "action": ActionType.ACTIVATE_PUMP.value,
                "params": {"pump_id": PUMP_ID, "mode": pump_mode},
            },
            {
                "id": "notify",
                "action": ActionType.NOTIFY_TRAFFIC_AGENTS.value,
                "params": {"message": message},
            },
        ]
    elif event.ambulance_detected:
        scenario = "ambulance-only"
        goal = "Create emergency corridor for ambulance"
        message = "Emergency corridor activated for ambulance"
        steps = [
            {
                "id": "notify",
                "action": ActionType.NOTIFY_TRAFFIC_AGENTS.value,
                "params": {"message": message},
            },
        ]
    elif flood_event:
        scenario = "flood-response"
        goal = "Activate drainage pumps for weather risk"
        message = "Drainage pump activated for weather risk"
        pump_mode = "high" if event.flood_risk else "auto"
        steps = [
            {
                "id": "read-pump",
                "action": ActionType.GET_PUMP_STATUS.value,
                "params": {"pump_id": PUMP_ID},
            },
            {
                "id": "activate-pump",
                "action": ActionType.ACTIVATE_PUMP.value,
                "params": {"pump_id": PUMP_ID, "mode": pump_mode},
            },
            {
                "id": "notify",
                "action": ActionType.NOTIFY_TRAFFIC_AGENTS.value,
                "params": {"message": message},
            },
        ]
    else:
        scenario = "baseline"
        goal = "Maintain normal traffic operation"
        message = "Traffic remains in normal mode"
        steps = [
            {
                "id": "notify",
                "action": ActionType.NOTIFY_TRAFFIC_AGENTS.value,
                "params": {"message": message},
            },
        ]

    return {
        "plan_id": str(uuid.uuid4()),
        "goal": goal,
        "scenario": scenario,
        "risk_level": risk_level.value,
        "steps": steps,
        "approval": {"autonomy_level": autonomy_level},
        "telemetry": {"traceId": trace_id},
    }


def _llm_planner_payload(
    event: MonitorEvent, trace_id: str
) -> Optional[Dict[str, Any]]:
    """
    Generate plan using LLM planner with LangChain.
    """

    # Use LangChain-based LLM planner
    llm_plan = generate_plan_with_llm(event, trace_id)
    if llm_plan:
        return llm_plan

    return None


def build_candidate_plan(event: MonitorEvent, trace_id: str) -> CandidatePlan:
    with stage_timer("plan", "planner") as timing:
        try:
            llm_payload = _llm_planner_payload(event, trace_id)
            plan_data = (
                llm_payload if llm_payload else _build_rule_based_plan(event, trace_id)
            )
            source = "llm" if llm_payload else "rule_based"
            plan = validate_plan_dict(plan_data)
        except Exception:
            ERRORS_TOTAL.labels(component="planner", kind="plan_build").inc()
            raise

    PLANS_TOTAL.labels(
        scenario=plan.scenario,
        risk_level=plan.risk_level.value,
        source=source,
    ).inc()

    logger.info(
        "Candidate plan generated",
        extra={
            "traceId": trace_id,
            "extra_fields": {
                "plan_id": plan.plan_id,
                "scenario": plan.scenario,
                "risk_level": plan.risk_level.value,
                "autonomy_level": plan.approval.autonomy_level,
                "source": source,
                "duration_ms": timing["duration_ms"],
            },
        },
    )
    record_event(
        component="planner",
        event_type="PLAN_CREATED",
        trace_id=trace_id,
        plan_id=plan.plan_id,
        actor=source,
        outcome="created",
        payload={
            "scenario": plan.scenario,
            "risk_level": plan.risk_level.value,
            "autonomy_level": plan.approval.autonomy_level,
            "goal": plan.goal,
            "steps": [
                {"id": s.id, "action": s.action.value, "params": s.params}
                for s in plan.steps
            ],
            "duration_ms": timing["duration_ms"],
            "event": {
                "event_type": event.event_type,
                "ambulance_detected": event.ambulance_detected,
                "heavy_rain": event.heavy_rain,
                "flood_risk": event.flood_risk,
                "crowd_level": event.crowd_level,
                "location": event.location,
                "weather_station_data": event.weather_station_data,
                "weather_forecast": event.weather_forecast,
                "user_permissions": event.user_permissions,
            },
        },
    )
    return plan


def malformed_plan_fixture(trace_id: str) -> Dict[str, Any]:
    """
    Malformed plan fixture is designed to test the robustness of the plan validation and execution system.
    """
    return {
        "plan_id": str(uuid.uuid4()),
        "goal": "Malformed plan fixture",
        "scenario": "test-malformed",
        "risk_level": "high",
        "steps": [
            {
                "id": "bad-step",
                "action": "activatePump",
                "params": {},
            }
        ],
        "approval": {"autonomy_level": 3},
        "telemetry": {"traceId": trace_id},
    }
