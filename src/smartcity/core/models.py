from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ActionType(str, Enum):
    NOTIFY_TRAFFIC_AGENTS = "notifyTrafficAgents"
    GET_PUMP_STATUS = "getPumpStatus"
    ACTIVATE_PUMP = "activatePump"
    DEACTIVATE_PUMP = "deactivatePump"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalMode(str, Enum):
    AUTO = "auto"
    HUMAN = "human"
    DENY = "deny"


class MonitorEvent(BaseModel):
    event_type: str = Field(default="combined")
    ambulance_detected: bool = Field(default=False)
    heavy_rain: bool = Field(default=False)
    flood_risk: bool = Field(default=False)
    crowd_level: str = Field(default="normal")
    location: str = Field(default="Avenue 1")
    notes: Optional[str] = None
    weather_station_data: Optional[Dict[str, Any]] = None
    weather_forecast: Optional[Dict[str, Any]] = None
    user_permissions: Optional[List[str]] = None


class PlanStep(BaseModel):
    id: str
    action: ActionType
    params: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_required_params(self) -> "PlanStep":
        required = {
            ActionType.NOTIFY_TRAFFIC_AGENTS: {"message"},
            ActionType.GET_PUMP_STATUS: {"pump_id"},
            ActionType.ACTIVATE_PUMP: {"pump_id", "mode"},
            ActionType.DEACTIVATE_PUMP: {"pump_id"},
        }
        required_keys = required[self.action]
        missing = sorted(k for k in required_keys if k not in self.params)
        if missing:
            raise ValueError(
                f"step '{self.id}' missing required params for '{self.action.value}': {', '.join(missing)}"
            )
        return self


class ApprovalRequest(BaseModel):
    autonomy_level: int = Field(ge=1, le=3)
    human_token: Optional[str] = None


class Telemetry(BaseModel):
    trace_id: str = Field(alias="traceId")
    model_config = ConfigDict(populate_by_name=True)


class CandidatePlan(BaseModel):
    plan_id: str
    goal: str
    scenario: str
    risk_level: RiskLevel
    steps: List[PlanStep] = Field(min_length=1)
    approval: ApprovalRequest
    telemetry: Telemetry

    model_config = ConfigDict(populate_by_name=True)

    def to_wire_dict(self) -> Dict[str, Any]:
        return self.model_dump(by_alias=True)

    @staticmethod
    def from_json(payload: str) -> "CandidatePlan":
        data = json.loads(payload)
        return CandidatePlan.model_validate(data)


class PolicyDecision(BaseModel):
    allowed: bool
    risk_level: RiskLevel
    approval_mode: ApprovalMode
    verdict_color: str
    reason: str
    source: str


class StepResult(BaseModel):
    step_id: str
    action: ActionType
    status_code: int
    response_body: str


class ExecutionReport(BaseModel):
    plan_id: str
    trace_id: str
    policy: PolicyDecision
    executed: bool
    step_results: List[StepResult] = Field(default_factory=list)
    error: Optional[str] = None


def validate_plan_dict(plan_dict: Dict[str, Any]) -> CandidatePlan:
    try:
        return CandidatePlan.model_validate(plan_dict)
    except ValidationError as exc:
        details = "; ".join(err.get("msg", "validation error") for err in exc.errors())
        raise ValueError(f"Invalid candidate plan: {details}") from exc
