"""
schemas.py — Pydantic v2 request/response schemas for RailBlock AI.

All time strings are validated to HH:MM format.

Time interval rules:
  - end > start  -> normal same-day interval
  - end < start  -> overnight interval
  - end == start -> invalid

All foreign key references use human-readable codes (block_id, dept code, etc.)
rather than numeric IDs — the API layer resolves them to DB IDs.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_hhmm(value: str, field: str) -> str:
    """Ensure value is a valid HH:MM time string."""
    try:
        parts = value.split(":")
        assert len(parts) == 2
        h, m = int(parts[0]), int(parts[1])
        assert 0 <= h <= 23 and 0 <= m <= 59
    except (AssertionError, ValueError, AttributeError):
        raise ValueError(
            f"{field} must be in HH:MM format (00:00–23:59), got '{value}'"
        )

    return value


def _validate_time_interval(
    start_time: str,
    end_time: str,
    start_field: str,
    end_field: str,
) -> None:
    """
    Validate a time interval.

    Overnight intervals are allowed.

    Examples:
        01:00 -> 04:00  valid
        23:00 -> 04:00  valid (overnight)
        23:30 -> 01:30  valid (overnight)
        04:00 -> 04:00  invalid
    """

    def to_min(t: str) -> int:
        h, m = map(int, t.split(":"))
        return h * 60 + m

    start_minutes = to_min(start_time)
    end_minutes = to_min(end_time)

    # Equal times are ambiguous and are rejected.
    #
    # If end < start, the interval is interpreted as crossing midnight.
    if end_minutes == start_minutes:
        raise ValueError(
            f"{end_field} must be different from {start_field}; "
            "equal times are not allowed"
        )


VALID_SOURCES = {"timetable", "goods_forecast"}
VALID_STATUSES = {"pending", "scheduled", "completed", "deferred", "cancelled"}
VALID_RESOURCE_TYPES = {"machine", "crew", "vehicle", "equipment"}
VALID_AVAILABILITY = {"available", "unavailable", "under_maintenance"}
VALID_HORIZONS = {"weekly", "monthly"}


# ===========================================================================
# Department
# ===========================================================================

class DepartmentCreate(BaseModel):
    """Create a department (ENG / SNT / TD)."""

    code: str = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Short department code, e.g. ENG, SNT, TD",
    )

    name: str = Field(
        ...,
        min_length=1,
        description="Full department name",
    )

    source_system: str = Field(
        ...,
        min_length=1,
        description="Source data system, e.g. TMS, SMMS, TDMS",
    )

    @field_validator("code")
    @classmethod
    def code_uppercase(cls, v: str) -> str:
        return v.strip().upper()


class DepartmentOut(BaseModel):
    id: int
    code: str
    name: str
    source_system: str

    class Config:
        from_attributes = True


# ===========================================================================
# Corridor
# ===========================================================================

class CorridorCreate(BaseModel):
    """Create a corridor/block section."""

    block_id: str = Field(
        ...,
        min_length=1,
        description="Unique block ID, e.g. B001",
    )

    block_name: str = Field(
        ...,
        min_length=1,
    )

    start_station: str = Field(
        ...,
        min_length=1,
    )

    end_station: str = Field(
        ...,
        min_length=1,
    )

    line_capacity_per_day: int = Field(
        default=1,
        ge=1,
        description="Number of train paths per day",
    )

    annual_gmt: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Annual Gross Million Tonnes (optional)",
    )

    @field_validator("block_id")
    @classmethod
    def block_id_uppercase(cls, v: str) -> str:
        return v.strip().upper()


class CorridorOut(BaseModel):
    id: int
    block_id: str
    block_name: str
    start_station: str
    end_station: str
    line_capacity_per_day: int
    annual_gmt: Optional[float]

    class Config:
        from_attributes = True


# ===========================================================================
# Resource
# ===========================================================================

class ResourceCreate(BaseModel):
    """Create a maintenance resource (machine or crew)."""

    resource_code: str = Field(
        ...,
        min_length=1,
        description="Unique resource code, e.g. TM-GZB-01",
    )

    resource_name: str = Field(
        ...,
        min_length=1,
        description="Human-readable name, e.g. 'Tamping Machine GZB-01'",
    )

    department_code: str = Field(
        ...,
        description="Department this resource belongs to",
    )

    home_depot: Optional[str] = Field(
        default=None,
        description="Home base / depot name",
    )

    resource_type: str = Field(
        ...,
        description="machine | crew | vehicle | equipment",
    )

    availability_status: str = Field(
        default="available",
        description="available | unavailable | under_maintenance",
    )

    @field_validator("department_code")
    @classmethod
    def dept_uppercase(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("resource_type")
    @classmethod
    def validate_resource_type(cls, v: str) -> str:
        v = v.lower().strip()

        if v not in VALID_RESOURCE_TYPES:
            raise ValueError(
                f"resource_type must be one of {VALID_RESOURCE_TYPES}"
            )

        return v

    @field_validator("availability_status")
    @classmethod
    def validate_availability(cls, v: str) -> str:
        v = v.lower().strip()

        if v not in VALID_AVAILABILITY:
            raise ValueError(
                f"availability_status must be one of {VALID_AVAILABILITY}"
            )

        return v


class ResourceOut(BaseModel):
    id: int
    resource_code: str
    resource_name: str
    department_code: str
    home_depot: Optional[str]
    resource_type: str
    availability_status: str

    class Config:
        from_attributes = True


# ===========================================================================
# Availability Window
# ===========================================================================

class AvailabilityWindowCreate(BaseModel):
    """
    Create a COA-derived availability window for a corridor.

    Overnight windows are allowed.

    Examples:
        01:00 -> 04:00  normal window
        23:00 -> 04:00  overnight window
    """

    corridor_block_id: str = Field(
        ...,
        description="Block ID of the corridor",
    )

    date: date

    start_time: str = Field(
        ...,
        description="Window start in HH:MM format",
    )

    end_time: str = Field(
        ...,
        description="Window end in HH:MM format",
    )

    is_goods_forecast_clear: bool = Field(
        default=True,
        description="True if no goods train is forecast during this window",
    )

    @field_validator("start_time")
    @classmethod
    def validate_start(cls, v: str) -> str:
        return _validate_hhmm(v, "start_time")

    @field_validator("end_time")
    @classmethod
    def validate_end(cls, v: str) -> str:
        return _validate_hhmm(v, "end_time")

    @model_validator(mode="after")
    def validate_interval(self) -> AvailabilityWindowCreate:
        _validate_time_interval(
            self.start_time,
            self.end_time,
            "start_time",
            "end_time",
        )

        return self


class AvailabilityWindowOut(BaseModel):
    id: int
    corridor_block_id: str
    date: date
    start_time: str
    end_time: str
    is_goods_forecast_clear: bool

    class Config:
        from_attributes = True


# ===========================================================================
# Train
# ===========================================================================

class TrainCreate(BaseModel):
    """Create a train (passenger or goods)."""

    train_id: str = Field(
        ...,
        min_length=1,
        description="Train number, e.g. '12001'",
    )

    train_name: str = Field(
        ...,
        min_length=1,
    )

    priority: int = Field(
        default=3,
        ge=1,
        le=5,
        description="1 = highest (Rajdhani), 5 = lowest (slow goods)",
    )


class TrainOut(BaseModel):
    id: int
    train_id: str
    train_name: str
    priority: int

    class Config:
        from_attributes = True


# ===========================================================================
# Train Occupancy
# ===========================================================================

class TrainOccupancyCreate(BaseModel):
    """
    Record a train occupying a corridor section — blocks maintenance.

    Overnight train occupancy is allowed.

    Examples:
        02:00 -> 03:00  normal occupancy
        23:30 -> 01:30  overnight occupancy
    """

    train_id: str = Field(
        ...,
        description="Train number matching an existing train",
    )

    corridor_block_id: str = Field(
        ...,
        description="Block ID of the corridor",
    )

    date: date

    entry_time: str = Field(
        ...,
        description="Train entry time HH:MM",
    )

    exit_time: str = Field(
        ...,
        description="Train exit time HH:MM",
    )

    source: str = Field(
        default="timetable",
        description="timetable | goods_forecast",
    )

    @field_validator("entry_time")
    @classmethod
    def validate_entry(cls, v: str) -> str:
        return _validate_hhmm(v, "entry_time")

    @field_validator("exit_time")
    @classmethod
    def validate_exit(cls, v: str) -> str:
        return _validate_hhmm(v, "exit_time")

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: str) -> str:
        v = v.lower().strip()

        if v not in VALID_SOURCES:
            raise ValueError(
                f"source must be one of {VALID_SOURCES}"
            )

        return v

    @model_validator(mode="after")
    def validate_interval(self) -> TrainOccupancyCreate:
        _validate_time_interval(
            self.entry_time,
            self.exit_time,
            "entry_time",
            "exit_time",
        )

        return self


class TrainOccupancyOut(BaseModel):
    id: int
    train_id: str
    corridor_block_id: str
    date: date
    entry_time: str
    exit_time: str
    source: str

    class Config:
        from_attributes = True


# ===========================================================================
# Maintenance Task
# ===========================================================================

class MaintenanceTaskCreate(BaseModel):
    """Create a maintenance task (unified format for TMS/SMMS/TDMS tasks)."""

    task_ref: str = Field(
        ...,
        min_length=1,
        description="Unique task reference, e.g. 'ENG-B001-001'",
    )

    department_code: str = Field(
        ...,
        description="Department code (ENG/SNT/TD)",
    )

    corridor_block_id: str = Field(
        ...,
        description="Block ID of the corridor",
    )

    required_resource_code: Optional[str] = Field(
        default=None,
        description="Optional: resource code of required machine/crew",
    )

    description: str = Field(
        ...,
        min_length=1,
    )

    defect_type: str = Field(
        ...,
        min_length=1,
        description="Type of defect, e.g. TRACK_TAMPING, OHE_REPLACEMENT",
    )

    criticality: int = Field(
        ...,
        ge=1,
        le=5,
        description="1 = most safety-critical, 5 = least critical",
    )

    reported_date: date

    due_date: date

    estimated_duration_minutes: int = Field(
        ...,
        gt=0,
        description="Estimated work duration (must be > 0)",
    )

    asset_impact_score: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="0–100 scale, domain expert assessment of asset impact if deferred",
    )

    @field_validator("department_code")
    @classmethod
    def dept_uppercase(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("defect_type")
    @classmethod
    def defect_uppercase(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def due_after_reported(self) -> MaintenanceTaskCreate:
        if self.due_date < self.reported_date:
            raise ValueError("due_date cannot be before reported_date")

        return self


class MaintenanceTaskOut(BaseModel):
    id: int
    task_ref: str
    department_code: str
    corridor_block_id: str
    required_resource_code: Optional[str]
    description: str
    defect_type: str
    criticality: int
    reported_date: date
    due_date: date
    estimated_duration_minutes: int
    asset_impact_score: float
    status: str

    class Config:
        from_attributes = True


class PrioritizedTaskOut(BaseModel):
    task_ref: str
    department: str
    corridor_block_id: str
    defect_type: str
    criticality: int
    due_date: date
    estimated_duration_minutes: int
    asset_impact_score: float
    priority_score: float
    overdue_days: int
    status: str

    class Config:
        from_attributes = True


# ===========================================================================
# Block Plan
# ===========================================================================

class BlockPlanEntry(BaseModel):
    """A single scheduled task entry in the generated block plan."""

    task_ref: str
    department: str
    corridor_block_id: str
    defect_type: str
    block_group_id: str
    scheduled_date: date
    entry_time: str
    exit_time: str
    priority_score: float
    resource_code: Optional[str] = None
    status: str  # "OPTIMALLY_SCHEDULED"

    class Config:
        from_attributes = True


class UnscheduledEntry(BaseModel):
    """A task that could not be scheduled, with a human-readable reason."""

    task_ref: str
    department: str
    corridor_block_id: str
    defect_type: str
    criticality: int
    due_date: date
    priority_score: float
    reason: str  # e.g. "no_availability_window", "insufficient_window_duration", etc.


class OptimizationSummary(BaseModel):
    solver_engine: str
    solver_status: str
    total_block_minutes: int
    asset_availability_score: float
    coordinated_tasks: int
    solve_time_ms: float


class BlockPlanResponse(BaseModel):
    """Full response from POST /generate-block-plan."""

    horizon: str
    solver_engine: str
    start_date: date
    end_date: date
    scheduled_count: int
    unscheduled_count: int
    optimization_summary: OptimizationSummary
    plan: list[BlockPlanEntry]
    unscheduled: list[UnscheduledEntry]


# ===========================================================================
# Generate Block Plan Request Body
# ===========================================================================

class GenerateBlockPlanRequest(BaseModel):
    """
    Optional request body for POST /generate-block-plan.

    incompatible_pairs: list of defect-type pairs that must NOT share a block.

    Example:
        [["TRACK_TAMPING", "OHE_REPLACEMENT"]]
    """

    incompatible_pairs: list[list[str]] = Field(
        default=[],
        description=(
            "List of defect_type pairs that are safety-incompatible. "
            "Each pair is a two-element list of defect type strings. "
            "Example: [[\"TRACK_TAMPING\", \"OHE_REPLACEMENT\"]]"
        ),
    )

    @field_validator("incompatible_pairs")
    @classmethod
    def validate_pairs(cls, v: list[list[str]]) -> list[list[str]]:
        for pair in v:
            if len(pair) != 2:
                raise ValueError(
                    "Each incompatible pair must be exactly two defect type strings"
                )

        return [
            [a.strip().upper(), b.strip().upper()]
            for a, b in v
        ]


# ===========================================================================
# Data Integration (Mock TMS / SMMS / TDMS / COA)
# ===========================================================================

class TmsTaskPayload(BaseModel):
    """Mock TMS (Engineering) task payload."""

    tms_ref: str
    section_id: str
    defect_description: str
    defect_code: str
    severity: int = Field(
        ...,
        ge=1,
        le=5,
    )
    logged_date: date
    target_date: date
    work_hours: float = Field(
        ...,
        gt=0,
    )
    track_impact_index: float = Field(
        default=0.0,
        ge=0.0,
    )


class SmmsTaskPayload(BaseModel):
    """Mock SMMS (Signal & Telecom) task payload."""

    smms_ref: str
    location_id: str
    fault_description: str
    fault_type: str
    priority_level: int = Field(
        ...,
        ge=1,
        le=5,
    )
    reported_on: date
    due_on: date
    est_duration_hrs: float = Field(
        ...,
        gt=0,
    )
    signal_impact_score: float = Field(
        default=0.0,
        ge=0.0,
    )


class TdmsTaskPayload(BaseModel):
    """Mock TDMS (Traction Distribution) task payload."""

    tdms_ref: str
    ohe_section: str
    issue_description: str
    issue_type: str
    urgency: int = Field(
        ...,
        ge=1,
        le=5,
    )
    date_reported: date
    completion_deadline: date
    duration_minutes: int = Field(
        ...,
        gt=0,
    )
    traction_impact: float = Field(
        default=0.0,
        ge=0.0,
    )


class CoaWindowPayload(BaseModel):
    """
    Mock COA (Control Office Application) availability window.

    Overnight windows are allowed.
    """

    block_section: str
    window_date: date
    from_time: str
    to_time: str
    goods_train_clear: bool = True

    @field_validator("from_time")
    @classmethod
    def validate_from(cls, v: str) -> str:
        return _validate_hhmm(v, "from_time")

    @field_validator("to_time")
    @classmethod
    def validate_to(cls, v: str) -> str:
        return _validate_hhmm(v, "to_time")

    @model_validator(mode="after")
    def validate_interval(self) -> CoaWindowPayload:
        _validate_time_interval(
            self.from_time,
            self.to_time,
            "from_time",
            "to_time",
        )

        return self


class CoaOccupancyPayload(BaseModel):
    """
    Mock COA train occupancy / goods forecast payload.

    Overnight occupancy is allowed.
    """

    train_number: str
    block_section: str
    occupancy_date: date
    arrival_time: str
    departure_time: str
    train_type: str = Field(
        default="passenger",
        description="passenger | goods | special",
    )

    @field_validator("arrival_time")
    @classmethod
    def validate_arrival(cls, v: str) -> str:
        return _validate_hhmm(v, "arrival_time")

    @field_validator("departure_time")
    @classmethod
    def validate_departure(cls, v: str) -> str:
        return _validate_hhmm(v, "departure_time")

    @model_validator(mode="after")
    def validate_interval(self) -> CoaOccupancyPayload:
        _validate_time_interval(
            self.arrival_time,
            self.departure_time,
            "arrival_time",
            "departure_time",
        )

        return self