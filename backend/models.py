"""
models.py — SQLAlchemy ORM models for RailBlock AI (SIH26027).

Stage 1 improvements:
  - Stronger defaults
  - Automatic timestamps
  - Database indexes for scheduling queries
  - Explicit foreign-key deletion behaviour
  - Improved relationship configuration
  - Clear domain constraints/documentation

Entities:
  DepartmentModel
  CorridorModel
  ResourceModel
  AvailabilityWindowModel
  TrainModel
  TrainOccupancyModel
  MaintenanceTaskModel
  BlockPlanModel

NOTE:
  Time fields intentionally remain String ("HH:MM") in Stage 1 so the
  existing schemas/services/optimizer remain compatible. Time migration
  will be handled only after those files are reviewed.
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    ForeignKey,
    Date,
    DateTime,
    Index,
)
from sqlalchemy.orm import relationship

from database import Base


# ===========================================================================
# Department
# ===========================================================================
# Represents:
#   ENG  -> Engineering / TMS
#   SNT  -> Signal & Telecom / SMMS
#   TD   -> Traction Distribution / TDMS
# ===========================================================================

class DepartmentModel(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True, index=True)

    code = Column(
        String,
        unique=True,
        index=True,
        nullable=False,
    )

    name = Column(
        String,
        nullable=False,
    )

    source_system = Column(
        String,
        nullable=False,
    )

    # Relationships
    resources = relationship(
        "ResourceModel",
        back_populates="department",
        passive_deletes=True,
    )

    tasks = relationship(
        "MaintenanceTaskModel",
        back_populates="department",
        passive_deletes=True,
    )


# ===========================================================================
# Corridor / Railway Block Section
# ===========================================================================

class CorridorModel(Base):
    __tablename__ = "corridors"

    id = Column(Integer, primary_key=True, index=True)

    # Example: B001
    block_id = Column(
        String,
        unique=True,
        index=True,
        nullable=False,
    )

    block_name = Column(
        String,
        nullable=False,
    )

    start_station = Column(
        String,
        nullable=False,
    )

    end_station = Column(
        String,
        nullable=False,
    )

    # Number of maintenance activities that can theoretically be handled
    # per day. Detailed simultaneous-use constraints are handled by the
    # optimizer.
    line_capacity_per_day = Column(
        Integer,
        default=1,
        nullable=False,
    )

    # Gross Million Tonnes — optional traffic-density metric.
    annual_gmt = Column(
        Float,
        nullable=True,
    )

    # Relationships
    windows = relationship(
        "AvailabilityWindowModel",
        back_populates="corridor",
        passive_deletes=True,
    )

    tasks = relationship(
        "MaintenanceTaskModel",
        back_populates="corridor",
        passive_deletes=True,
    )

    occupancies = relationship(
        "TrainOccupancyModel",
        back_populates="corridor",
        passive_deletes=True,
    )


# ===========================================================================
# Maintenance Resource
# ===========================================================================
# Examples:
#   Track machine
#   Signal crew
#   OHE crew
#   Maintenance vehicle
# ===========================================================================

class ResourceModel(Base):
    __tablename__ = "resources"

    id = Column(Integer, primary_key=True, index=True)

    resource_code = Column(
        String,
        unique=True,
        index=True,
        nullable=False,
    )

    resource_name = Column(
        String,
        nullable=False,
    )

    department_id = Column(
        Integer,
        ForeignKey(
            "departments.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )

    home_depot = Column(
        String,
        nullable=True,
    )

    # machine | crew | vehicle
    resource_type = Column(
        String,
        nullable=False,
    )

    # available | unavailable | under_maintenance
    availability_status = Column(
        String,
        default="available",
        nullable=False,
        index=True,
    )

    # Relationships
    department = relationship(
        "DepartmentModel",
        back_populates="resources",
    )

    tasks = relationship(
        "MaintenanceTaskModel",
        back_populates="required_resource",
        passive_deletes=True,
    )


# ===========================================================================
# Availability Window
# ===========================================================================
# Represents a period during which a corridor is available for maintenance.
#
# Example:
#   B001
#   2026-08-27
#   01:00 -> 04:00
#
# Time values intentionally remain strings in Stage 1 for compatibility with
# the existing API/schema/optimizer.
# ===========================================================================

class AvailabilityWindowModel(Base):
    __tablename__ = "availability_windows"

    id = Column(Integer, primary_key=True, index=True)

    corridor_id = Column(
        Integer,
        ForeignKey(
            "corridors.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )

    date = Column(
        Date,
        index=True,
        nullable=False,
    )

    start_time = Column(
        String,
        nullable=False,
    )

    end_time = Column(
        String,
        nullable=False,
    )

    # False means a goods-train forecast blocks this window.
    is_goods_forecast_clear = Column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
    )

    corridor = relationship(
        "CorridorModel",
        back_populates="windows",
    )

    __table_args__ = (
        Index(
            "ix_availability_corridor_date",
            "corridor_id",
            "date",
        ),
    )


# ===========================================================================
# Train
# ===========================================================================

class TrainModel(Base):
    __tablename__ = "trains"

    id = Column(Integer, primary_key=True, index=True)

    # Example: 12001
    train_id = Column(
        String,
        unique=True,
        index=True,
        nullable=False,
    )

    train_name = Column(
        String,
        nullable=False,
    )

    # Priority:
    #   1 = highest
    #   5 = lowest
    priority = Column(
        Integer,
        default=3,
        nullable=False,
        index=True,
    )

    occupancies = relationship(
        "TrainOccupancyModel",
        back_populates="train",
        passive_deletes=True,
    )


# ===========================================================================
# Train Occupancy
# ===========================================================================
# Represents the period during which a train occupies a corridor.
#
# source:
#   timetable
#   goods_forecast
#
# Maintenance must not overlap with an occupancy period.
# ===========================================================================

class TrainOccupancyModel(Base):
    __tablename__ = "train_occupancy"

    id = Column(Integer, primary_key=True, index=True)

    train_id = Column(
        Integer,
        ForeignKey(
            "trains.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )

    corridor_id = Column(
        Integer,
        ForeignKey(
            "corridors.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )

    date = Column(
        Date,
        index=True,
        nullable=False,
    )

    

    entry_time = Column(
        String,
        nullable=False,
    )

    exit_time = Column(
        String,
        nullable=False,
    )

    # timetable | goods_forecast
    source = Column(
        String,
        default="timetable",
        nullable=False,
        index=True,
    )

    train = relationship(
        "TrainModel",
        back_populates="occupancies",
    )

    corridor = relationship(
        "CorridorModel",
        back_populates="occupancies",
    )

    __table_args__ = (
        Index(
            "ix_train_occupancy_corridor_date",
            "corridor_id",
            "date",
        ),
        Index(
            "ix_train_occupancy_train_date",
            "train_id",
            "date",
        ),
    )


# ===========================================================================
# Maintenance Task
# ===========================================================================
# Unified representation of:
#   TMS  -> Engineering
#   SMMS -> Signal & Telecom
#   TDMS -> Traction Distribution
#
# Criticality:
#   1 = highest safety risk
#   5 = lowest safety risk
#
# Status lifecycle:
#   pending
#      ↓
#   scheduled
#      ↓
#   completed
#
# Alternative:
#   pending -> deferred
#   pending -> cancelled
# ===========================================================================

class MaintenanceTaskModel(Base):
    __tablename__ = "maintenance_tasks"

    id = Column(Integer, primary_key=True, index=True)

    task_ref = Column(
        String,
        unique=True,
        index=True,
        nullable=False,
    )

    department_id = Column(
        Integer,
        ForeignKey(
            "departments.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )

    corridor_id = Column(
        Integer,
        ForeignKey(
            "corridors.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )

    # Optional specific resource required for the task.
    required_resource_id = Column(
        Integer,
        ForeignKey(
            "resources.id",
            ondelete="RESTRICT",
        ),
        nullable=True,
        index=True,
    )

    description = Column(
        String,
        nullable=False,
    )

    # Examples:
    #   TRACK_TAMPING
    #   OHE_REPLACEMENT
    #   SIGNAL_REPAIR
    defect_type = Column(
        String,
        nullable=False,
        index=True,
    )

    # 1 = highest safety risk
    # 5 = lowest safety risk
    criticality = Column(
        Integer,
        nullable=False,
        index=True,
    )

    reported_date = Column(
        Date,
        nullable=False,
        index=True,
    )

    due_date = Column(
        Date,
        nullable=False,
        index=True,
    )

    estimated_duration_minutes = Column(
        Integer,
        nullable=False,
    )

    # 0–100 domain-expert impact score.
    asset_impact_score = Column(
        Float,
        default=0.0,
        nullable=False,
    )

    # pending | scheduled | completed | deferred | cancelled
    status = Column(
        String,
        default="pending",
        nullable=False,
        index=True,
    )

    # Useful for auditability and dashboard reporting.
    created_at = Column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    # Relationships
    department = relationship(
        "DepartmentModel",
        back_populates="tasks",
    )

    corridor = relationship(
        "CorridorModel",
        back_populates="tasks",
    )

    required_resource = relationship(
        "ResourceModel",
        back_populates="tasks",
    )

    block_plans = relationship(
        "BlockPlanModel",
        back_populates="task",
        passive_deletes=True,
    )

    __table_args__ = (
        Index(
            "ix_maintenance_status_due_date",
            "status",
            "due_date",
        ),
        Index(
            "ix_maintenance_corridor_status",
            "corridor_id",
            "status",
        ),
    )


# ===========================================================================
# Block Plan
# ===========================================================================
# Output of the CP-SAT optimizer.
#
# One row represents one maintenance task scheduled inside a corridor
# availability window.
#
# Tasks sharing block_group_id can represent coordinated/parallel
# cross-department work in the same physical block.
# ===========================================================================

class BlockPlanModel(Base):
    __tablename__ = "block_plans"

    id = Column(Integer, primary_key=True, index=True)

    # A maintenance task should have at most one active generated plan.
    task_id = Column(
        Integer,
        ForeignKey(
            "maintenance_tasks.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        unique=True,
        index=True,
    )

    corridor_id = Column(
        Integer,
        ForeignKey(
            "corridors.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )

    availability_window_id = Column(
        Integer,
        ForeignKey(
            "availability_windows.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )

    resource_id = Column(
        Integer,
        ForeignKey(
            "resources.id",
            ondelete="RESTRICT",
        ),
        nullable=True,
        index=True,
    )

    # Tasks sharing this identifier are coordinated within the same
    # physical block.
    block_group_id = Column(
        String,
        index=True,
        nullable=False,
    )

    scheduled_date = Column(
        Date,
        index=True,
        nullable=False,
    )

    entry_time = Column(
        String,
        nullable=False,
    )

    exit_time = Column(
        String,
        nullable=False,
    )

    priority_score = Column(
        Float,
        nullable=False,
    )

    # weekly | monthly
    horizon = Column(
        String,
        nullable=False,
        index=True,
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True,
    )

    # Relationships
    task = relationship(
        "MaintenanceTaskModel",
        back_populates="block_plans",
    )

    corridor = relationship(
        "CorridorModel",
    )

    availability_window = relationship(
        "AvailabilityWindowModel",
    )

    resource = relationship(
        "ResourceModel",
    )

    __table_args__ = (
        Index(
            "ix_block_plan_date_corridor",
            "scheduled_date",
            "corridor_id",
        ),
        Index(
            "ix_block_plan_horizon_date",
            "horizon",
            "scheduled_date",
        ),
    )