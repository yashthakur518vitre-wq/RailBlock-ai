"""
services/scheduling_service.py — Block plan orchestration service.

Sits between the API layer and the CP-SAT optimizer:
  1. Fetches and prepares data from the database.
  2. Calls the CP-SAT optimizer.
  3. Persists the optimized block plan to the database.
  4. Updates task statuses.
  5. Builds the API response.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from optimizer.cp_sat_optimizer import run_cp_sat_block_planning
from services.priority_service import compute_priority_score

HORIZON_DAYS = {"weekly": 7, "monthly": 30}
SOLVER_ENGINE = "Google OR-Tools CP-SAT"


def _optional_attr(instance, name: str, default=None):
    """Read optional schema fields without breaking older DB models."""
    return getattr(instance, name, default)


def generate_block_plan(
    horizon: str,
    db: Session,
    incompatible_pairs_raw: list[list[str]],
    regenerate: bool,
) -> dict:
    """Generate, persist, and return a railway maintenance block plan."""

    if horizon not in HORIZON_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid horizon '{horizon}'. Must be 'weekly' or 'monthly'.",
        )

    today = date.today()
    horizon_days = HORIZON_DAYS[horizon]
    horizon_end = today + timedelta(days=horizon_days - 1)

    # ------------------------------------------------------------------
    # Guard: existing plans
    # ------------------------------------------------------------------
    existing_count = (
        db.query(models.BlockPlanModel)
        .filter(models.BlockPlanModel.horizon == horizon)
        .count()
    )

    if existing_count > 0:
        if not regenerate:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{existing_count} block plan entries already exist for horizon "
                    f"'{horizon}'. Use ?regenerate=true to clear and regenerate."
                ),
            )
        _reset_horizon_plans(horizon, db)

    # ------------------------------------------------------------------
    # Fetch pending tasks
    # ------------------------------------------------------------------
    pending_tasks_orm = (
        db.query(models.MaintenanceTaskModel)
        .filter(models.MaintenanceTaskModel.status == "pending")
        .all()
    )

    if not pending_tasks_orm:
        # If regenerate cleared existing plans, the reset must still be
        # committed even when there are no tasks left to schedule.
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Database error committing empty block-plan reset: {exc}",
            ) from exc

        return _build_empty_response(
            horizon,
            today,
            horizon_end,
            "No pending tasks found.",
        )

    tasks_input: list[dict] = []
    for task in pending_tasks_orm:
        score = compute_priority_score(
            criticality=task.criticality,
            due_date=task.due_date,
            asset_impact_score=task.asset_impact_score,
            as_of=today,
        )

        tasks_input.append(
            {
                "id": task.id,
                "task_ref": task.task_ref,
                "department_id": task.department_id,
                "department_code": task.department.code,
                "corridor_id": task.corridor_id,
                "corridor_block_id": task.corridor.block_id,
                "defect_type": task.defect_type,
                "estimated_duration_minutes": task.estimated_duration_minutes,
                "priority_score": score,
                "required_resource_id": task.required_resource_id,
                "due_date": task.due_date,
                "criticality": task.criticality,
                "asset_impact_score": task.asset_impact_score,
            }
        )

    tasks_input.sort(key=lambda item: item["priority_score"], reverse=True)

    # ------------------------------------------------------------------
    # Availability windows
    # ------------------------------------------------------------------
    # GOODS FORECAST POLICY:
    # A non-clear goods forecast is treated as a hard operational exclusion.
    # Therefore only clear windows reach CP-SAT. Do not describe freight
    # impact as a soft choice unless this policy is deliberately changed.
    windows_orm = (
        db.query(models.AvailabilityWindowModel)
        .filter(
            models.AvailabilityWindowModel.date >= today,
            models.AvailabilityWindowModel.date <= horizon_end,
            models.AvailabilityWindowModel.is_goods_forecast_clear.is_(True),
        )
        .order_by(
            models.AvailabilityWindowModel.date,
            models.AvailabilityWindowModel.start_time,
        )
        .all()
    )

    windows_input: list[dict] = []
    for window in windows_orm:
        windows_input.append(
            {
                "id": window.id,
                "corridor_id": window.corridor_id,
                "corridor_block_id": window.corridor.block_id,
                "date": window.date,
                "start_time": window.start_time,
                "end_time": window.end_time,
                "is_goods_forecast_clear": window.is_goods_forecast_clear,
                # Optional future forecast fields. They are harmless when
                # the current DB schema does not contain them.
                "expected_goods_trains": _optional_attr(
                    window, "expected_goods_trains", None
                ),
                "freight_probability": _optional_attr(
                    window, "freight_probability", None
                ),
                "forecast_confidence": _optional_attr(
                    window, "forecast_confidence", None
                ),
            }
        )

    # Remove optional keys whose value is None so the optimizer's backward
    # compatibility logic continues to behave as intended.
    for window in windows_input:
        for key in ("expected_goods_trains", "freight_probability", "forecast_confidence"):
            if window.get(key) is None:
                window.pop(key, None)

    # ------------------------------------------------------------------
    # Train occupancies
    # ------------------------------------------------------------------
    # Overnight occupancy is split inside the optimizer so a train crossing
    # midnight blocks both dates correctly.
    occupancies_orm = (
        db.query(models.TrainOccupancyModel)
        .filter(
            models.TrainOccupancyModel.date >= today,
            models.TrainOccupancyModel.date <= horizon_end,
        )
        .all()
    )

    occupancies_input = [
        {
            "corridor_id": occupancy.corridor_id,
            "date": occupancy.date,
            "entry_time": occupancy.entry_time,
            "exit_time": occupancy.exit_time,
            "source": occupancy.source,
        }
        for occupancy in occupancies_orm
    ]

    # NOTE: If a train starts on horizon_end and runs into the next day, the
    # current query still includes its starting date, which is enough to
    # block the horizon-end portion. A future-day maintenance window is not
    # part of this horizon.

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------
    resources_orm = db.query(models.ResourceModel).all()
    resources_input = [
        {
            "id": resource.id,
            "resource_code": resource.resource_code,
            "availability_status": resource.availability_status,
        }
        for resource in resources_orm
    ]

    # ------------------------------------------------------------------
    # Corridor capacities
    # ------------------------------------------------------------------
    corridors_orm = db.query(models.CorridorModel).all()
    corridor_capacities = {
        corridor.id: max(1, int(corridor.line_capacity_per_day or 1))
        for corridor in corridors_orm
    }

    # ------------------------------------------------------------------
    # Safety incompatibilities
    # ------------------------------------------------------------------
    incompatible_pairs: set[tuple[str, str]] = {
        (str(pair[0]).upper(), str(pair[1]).upper())
        for pair in incompatible_pairs_raw
        if len(pair) == 2
    }

    # ------------------------------------------------------------------
    # Run CP-SAT
    # ------------------------------------------------------------------
    try:
        result = run_cp_sat_block_planning(
            tasks=tasks_input,
            windows=windows_input,
            train_occupancies=occupancies_input,
            incompatible_pairs=incompatible_pairs,
            resources=resources_input,
            corridor_capacities=corridor_capacities,
            today=today,
            horizon_str=horizon,
        )
    except Exception:
        # Regeneration may have changed the current transaction. Roll it back
        # so a solver/model error never leaves a partial reset in the session.
        db.rollback()
        raise

    # ------------------------------------------------------------------
    # Persist scheduled entries
    # ------------------------------------------------------------------
    created_at = datetime.utcnow()
    task_orm_by_id = {task.id: task for task in pending_tasks_orm}

    for entry in result["scheduled"]:
        task_orm = task_orm_by_id.get(entry["task_id"])
        if task_orm is None:
            db.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Optimizer returned unknown task_id {entry['task_id']}.",
            )

        # Defensive guard. Under normal operation the regenerate/reset and
        # pending-task query make this unnecessary, but it protects against
        # stale/duplicate database state.
        existing_plan = (
            db.query(models.BlockPlanModel)
            .filter(models.BlockPlanModel.task_id == task_orm.id)
            .first()
        )
        if existing_plan:
            continue

        plan_row = models.BlockPlanModel(
            task_id=task_orm.id,
            corridor_id=task_orm.corridor_id,
            availability_window_id=entry["window_id"],
            resource_id=entry.get("resource_id"),
            block_group_id=entry["block_group_id"],
            scheduled_date=entry["scheduled_date"],
            entry_time=entry["entry_time"],
            exit_time=entry["exit_time"],
            priority_score=entry["priority_score"],
            horizon=horizon,
            created_at=created_at,
        )
        db.add(plan_row)
        task_orm.status = "scheduled"

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Database error while persisting block plan: {exc}",
        ) from exc

    # ------------------------------------------------------------------
    # API response
    # ------------------------------------------------------------------
    summary = result["optimization_summary"]

    return {
        "horizon": horizon,
        "solver_engine": SOLVER_ENGINE,
        "start_date": str(today),
        "end_date": str(horizon_end),
        "scheduled_count": len(result["scheduled"]),
        "unscheduled_count": len(result["unscheduled"]),
        "optimization_summary": {
            "solver_engine": SOLVER_ENGINE,
            "solver_status": summary["solver_status"],
            "total_block_minutes": summary["total_block_minutes"],
            "total_blocks": summary.get("total_blocks", 0),
            "asset_availability_score": summary["asset_availability_score"],
            "coordinated_tasks": summary["coordinated_tasks"],
            "shared_blocks": summary.get("shared_blocks", 0),
            "overdue_tasks_scheduled": summary.get("overdue_tasks_scheduled", 0),
            "average_scheduled_priority": summary.get("average_scheduled_priority", 0.0),
            "solve_time_ms": summary["solve_time_ms"],
        },
        "plan": [
            {
                "task_ref": entry["task_ref"],
                "department": entry["department_code"],
                "corridor_block_id": entry["corridor_block_id"],
                "defect_type": entry["defect_type"],
                "block_group_id": entry["block_group_id"],
                "scheduled_date": str(entry["scheduled_date"]),
                "entry_time": entry["entry_time"],
                "exit_time": entry["exit_time"],
                "priority_score": round(entry["priority_score"], 2),
                "status": entry["status"],
            }
            for entry in result["scheduled"]
        ],
        "unscheduled": [
            {
                "task_ref": item["task_ref"],
                "department": item["department_code"],
                "corridor_block_id": item["corridor_block_id"],
                "defect_type": item["defect_type"],
                "criticality": item["criticality"],
                "due_date": str(item["due_date"]),
                "priority_score": round(item["priority_score"], 2),
                "reason": item["reason"],
            }
            for item in result["unscheduled"]
        ],
    }


def _reset_horizon_plans(horizon: str, db: Session) -> None:
    """Delete block plans for a horizon and reset affected tasks to pending.

    This function deliberately does not commit. The caller commits only after
    the complete regeneration succeeds.
    """
    scheduled_task_ids = [
        row[0]
        for row in db.query(models.BlockPlanModel.task_id)
        .filter(models.BlockPlanModel.horizon == horizon)
        .distinct()
    ]

    db.query(models.BlockPlanModel).filter(
        models.BlockPlanModel.horizon == horizon
    ).delete(synchronize_session=False)

    if scheduled_task_ids:
        db.query(models.MaintenanceTaskModel).filter(
            models.MaintenanceTaskModel.id.in_(scheduled_task_ids)
        ).update(
            {"status": "pending"},
            synchronize_session=False,
        )


def reset_all_plans(db: Session) -> dict:
    """Reset ALL block plans and affected tasks back to pending."""
    scheduled_task_ids = [
        row[0]
        for row in db.query(models.BlockPlanModel.task_id).distinct()
    ]

    db.query(models.BlockPlanModel).delete(synchronize_session=False)

    if scheduled_task_ids:
        db.query(models.MaintenanceTaskModel).filter(
            models.MaintenanceTaskModel.id.in_(scheduled_task_ids)
        ).update(
            {"status": "pending"},
            synchronize_session=False,
        )

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Database error during block plan reset: {exc}",
        ) from exc

    return {
        "message": (
            f"Reset {len(scheduled_task_ids)} task(s) back to pending, "
            "cleared all block plan entries."
        ),
        "tasks_reset": len(scheduled_task_ids),
    }


def _build_empty_response(
    horizon: str,
    start_date: date,
    end_date: date,
    solver_status: str,
) -> dict:
    return {
        "horizon": horizon,
        "solver_engine": SOLVER_ENGINE,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "scheduled_count": 0,
        "unscheduled_count": 0,
        "optimization_summary": {
            "solver_engine": SOLVER_ENGINE,
            "solver_status": solver_status,
            "total_block_minutes": 0,
            "total_blocks": 0,
            "asset_availability_score": 100.0,
            "coordinated_tasks": 0,
            "shared_blocks": 0,
            "overdue_tasks_scheduled": 0,
            "average_scheduled_priority": 0.0,
            "solve_time_ms": 0.0,
        },
        "plan": [],
        "unscheduled": [],
    }
