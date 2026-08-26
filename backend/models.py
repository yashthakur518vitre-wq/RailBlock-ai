"""
models.py — SQLAlchemy ORM models for RailBlock AI (SIH26027).

Entities:
  DepartmentModel      — ENG / SNT / TD departments with source system mapping
  CorridorModel        — Railway corridor / block sections
  ResourceModel        — Track machines, signal crews, OHE crews (NEW)
  AvailabilityWindowModel — COA-derived maintenance windows
  TrainModel           — Train master data
  TrainOccupancyModel  — Timetable + goods-forecast occupancy records
  MaintenanceTaskModel — Defects/tasks from TMS, SMMS, TDMS
  BlockPlanModel       — CP-SAT generated block schedule output
"""

from sqlalchemy import (
    Column, Integer, String, Float, Boolean,
    ForeignKey, Date, DateTime
)
from sqlalchemy.orm import relationship
from database import Base


# ---------------------------------------------------------------------------
# Department
# Represents ENG (Engineering/TMS), SNT (Signal & Telecom/SMMS),
# TD (Traction Distribution/TDMS)
# ---------------------------------------------------------------------------

class DepartmentModel(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True)   # ENG | SNT | TD
    name = Column(String, nullable=False)
    source_system = Column(String, nullable=False)    # TMS | SMMS | TDMS

    # Relationships (back-references for convenience)
    resources = relationship("ResourceModel", back_populates="department")
    tasks = relationship("MaintenanceTaskModel", back_populates="department")


# ---------------------------------------------------------------------------
# Corridor (Railway Block Section)
# A corridor is a contiguous track section identified by a Block ID,
# matching the Control Office Application (COA) naming convention.
# ---------------------------------------------------------------------------

class CorridorModel(Base):
    __tablename__ = "corridors"

    id = Column(Integer, primary_key=True, index=True)
    block_id = Column(String, unique=True, index=True)  # e.g. "B001"
    block_name = Column(String, nullable=False)
    start_station = Column(String, nullable=False)
    end_station = Column(String, nullable=False)
    line_capacity_per_day = Column(Integer, default=1)
    # Gross Million Tonnes — optional traffic density metric
    annual_gmt = Column(Float, nullable=True)

    # Relationships
    windows = relationship("AvailabilityWindowModel", back_populates="corridor")
    tasks = relationship("MaintenanceTaskModel", back_populates="corridor")
    occupancies = relationship("TrainOccupancyModel", back_populates="corridor")


# ---------------------------------------------------------------------------
# Resource (Track machines, maintenance crews)
# Each resource belongs to one department and has a home depot.
# Resource availability is checked during block plan generation to prevent
# the same machine/crew being double-booked.
# ---------------------------------------------------------------------------

class ResourceModel(Base):
    __tablename__ = "resources"

    id = Column(Integer, primary_key=True, index=True)
    resource_code = Column(String, unique=True, index=True)  # e.g. "TM-GZB-01"
    resource_name = Column(String, nullable=False)            # "Tamping Machine GZB-01"
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    home_depot = Column(String, nullable=True)                # "Ghaziabad"
    resource_type = Column(String, nullable=False)            # "machine" | "crew" | "vehicle"
    # "available" | "unavailable" | "under_maintenance"
    availability_status = Column(String, default="available")

    department = relationship("DepartmentModel", back_populates="resources")
    tasks = relationship("MaintenanceTaskModel", back_populates="required_resource")


# ---------------------------------------------------------------------------
# Availability Window
# Derived from Control Office Application (COA) data — represents a time
# window when a corridor section is free from train operations and can be
# used for maintenance. goods_forecast_clear=True means no goods train
# is expected during this window.
# ---------------------------------------------------------------------------

class AvailabilityWindowModel(Base):
    __tablename__ = "availability_windows"

    id = Column(Integer, primary_key=True, index=True)
    corridor_id = Column(Integer, ForeignKey("corridors.id"), nullable=False)
    date = Column(Date, index=True, nullable=False)
    start_time = Column(String, nullable=False)   # "HH:MM"
    end_time = Column(String, nullable=False)     # "HH:MM"
    # False if a goods train forecast occupies this window
    is_goods_forecast_clear = Column(Boolean, default=True)

    corridor = relationship("CorridorModel", back_populates="windows")


# ---------------------------------------------------------------------------
# Train (Passenger / Goods master)
# Priority 1 = highest (e.g. Rajdhani Express), 5 = lowest (slow goods).
# ---------------------------------------------------------------------------

class TrainModel(Base):
    __tablename__ = "trains"

    id = Column(Integer, primary_key=True, index=True)
    train_id = Column(String, unique=True, index=True)  # "12001"
    train_name = Column(String, nullable=False)
    priority = Column(Integer, default=3)               # 1 (highest) – 5 (lowest)

    occupancies = relationship("TrainOccupancyModel", back_populates="train")


# ---------------------------------------------------------------------------
# Train Occupancy
# Each record means a specific train occupies a corridor section during a
# given date/time range. Source can be "timetable" (fixed schedule) or
# "goods_forecast" (dynamic goods train forecast from COA).
# Maintenance must NEVER be scheduled during any occupancy window.
# ---------------------------------------------------------------------------

class TrainOccupancyModel(Base):
    __tablename__ = "train_occupancy"

    id = Column(Integer, primary_key=True, index=True)
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=False)
    corridor_id = Column(Integer, ForeignKey("corridors.id"), nullable=False)
    date = Column(Date, index=True, nullable=False)
    entry_time = Column(String, nullable=False)   # "HH:MM"
    exit_time = Column(String, nullable=False)    # "HH:MM"
    # "timetable" | "goods_forecast"
    source = Column(String, default="timetable")

    train = relationship("TrainModel", back_populates="occupancies")
    corridor = relationship("CorridorModel", back_populates="occupancies")


# ---------------------------------------------------------------------------
# Maintenance Task
# Unified representation of defects/tasks imported from:
#   TMS  (Engineering track defects)
#   SMMS (Signal & Telecom faults)
#   TDMS (Traction / OHE defects)
#
# Criticality: 1 = highest safety risk, 5 = lowest.
# Status lifecycle: pending → scheduled → completed | deferred | cancelled
# ---------------------------------------------------------------------------

class MaintenanceTaskModel(Base):
    __tablename__ = "maintenance_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_ref = Column(String, unique=True, index=True)  # e.g. "ENG-B001-001"
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False)
    corridor_id = Column(Integer, ForeignKey("corridors.id"), nullable=False)

    # Optional: specific resource (machine/crew) required for this task
    required_resource_id = Column(
        Integer, ForeignKey("resources.id"), nullable=True
    )

    description = Column(String, nullable=False)
    defect_type = Column(String, nullable=False)   # e.g. "TRACK_TAMPING", "OHE_REPLACEMENT"
    criticality = Column(Integer, nullable=False)  # 1 (most critical) – 5 (least)
    reported_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False)
    estimated_duration_minutes = Column(Integer, nullable=False)
    asset_impact_score = Column(Float, default=0.0)  # 0–100 scale (domain expert input)

    # pending | scheduled | completed | deferred | cancelled
    status = Column(String, default="pending", index=True)

    department = relationship("DepartmentModel", back_populates="tasks")
    corridor = relationship("CorridorModel", back_populates="tasks")
    required_resource = relationship("ResourceModel", back_populates="tasks")
    block_plans = relationship("BlockPlanModel", back_populates="task")


# ---------------------------------------------------------------------------
# Block Plan
# The output of the CP-SAT optimizer. Each row represents one maintenance
# task scheduled in a specific corridor block.
# Tasks sharing the same block_group_id run IN PARALLEL in the same physical
# corridor block (possibly from multiple departments).
# ---------------------------------------------------------------------------

class BlockPlanModel(Base):
    __tablename__ = "block_plans"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("maintenance_tasks.id"), nullable=False, unique=True)
    corridor_id = Column(Integer, ForeignKey("corridors.id"), nullable=False)
    availability_window_id = Column(
        Integer, ForeignKey("availability_windows.id"), nullable=False
    )
    # Optional: which resource is assigned to this block plan entry
    resource_id = Column(Integer, ForeignKey("resources.id"), nullable=True)

    # Tasks sharing a block_group_id are running IN PARALLEL in the same
    # physical corridor block — this enables cross-department coordination.
    block_group_id = Column(String, index=True, nullable=False)

    scheduled_date = Column(Date, index=True, nullable=False)
    entry_time = Column(String, nullable=False)   # "HH:MM"
    exit_time = Column(String, nullable=False)    # "HH:MM"

    priority_score = Column(Float, nullable=False)
    horizon = Column(String, nullable=False)      # "weekly" | "monthly"
    created_at = Column(DateTime, nullable=False)

    task = relationship("MaintenanceTaskModel", back_populates="block_plans")
    corridor = relationship("CorridorModel")
    availability_window = relationship("AvailabilityWindowModel")
    resource = relationship("ResourceModel")