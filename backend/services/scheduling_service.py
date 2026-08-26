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


def generate_block_plan(
    horizon: str,
    db: Session,
    incompatible_pairs_raw: list[list[str]],
    regenerate: bool,
) -> dict:
    """
    Full block plan generation pipeline.

    1. Guard against duplicate generation (unless regenerate=True).
    2. Fetch pending tasks.
    3. Fetch availability windows in horizon.
    4. Fetch train occupancies in horizon.
    5. Fetch resources.
    6. Run CP-SAT optimizer.
    7. Persist results and update task statuses.
    8. Return formatted API response.
    """
    if horizon not in HORIZON_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid horizon '{horizon}'. Must be 'weekly' or 'monthly'."
        )

    today = date.today()
    horizon_days = HORIZON_DAYS[horizon]
    horizon_end = today + timedelta(days=horizon_days)

    # ------------------------------------------------------------------
    # Guard: check for existing plans
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
        return _build_empty_response(horizon, today, horizon_end, "No pending tasks found.")

    tasks_input: list[dict] = []
    for t in pending_tasks_orm:
        score = compute_priority_score(
            criticality=t.criticality,
            due_date=t.due_date,
            asset_impact_score=t.asset_impact_score,
            as_of=today,
        )
        tasks_input.append({
            "id": t.id,
            "task_ref": t.task_ref,
            "department_id": t.department_id,
            "department_code": t.department.code,
            "corridor_id": t.corridor_id,
            "corridor_block_id": t.corridor.block_id,
            "defect_type": t.defect_type,
            "estimated_duration_minutes": t.estimated_duration_minutes,
            "priority_score": score,
            "required_resource_id": t.required_resource_id,
            "due_date": t.due_date,
            "criticality": t.criticality,
            "asset_impact_score": t.asset_impact_score,
        })

    # Sort by priority score descending (highest priority first)
    tasks_input.sort(key=lambda t: t["priority_score"], reverse=True)

    # ------------------------------------------------------------------
    # Fetch availability windows in horizon (goods-forecast-clear only)
    # ------------------------------------------------------------------
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

    windows_input: list[dict] = [
        {
            "id": w.id,
            "corridor_id": w.corridor_id,
            "corridor_block_id": w.corridor.block_id,
            "date": w.date,
            "start_time": w.start_time,
            "end_time": w.end_time,
            "is_goods_forecast_clear": w.is_goods_forecast_clear,
        }
        for w in windows_orm
    ]

    # ------------------------------------------------------------------
    # Fetch train occupancies in horizon
    # Maintenance must NEVER overlap timetable OR goods-forecast occupancy.
    # ------------------------------------------------------------------
    occupancies_orm = (
        db.query(models.TrainOccupancyModel)
        .filter(
            models.TrainOccupancyModel.date >= today,
            models.TrainOccupancyModel.date <= horizon_end,
        )
        .all()
    )

    occupancies_input: list[dict] = [
        {
            "corridor_id": o.corridor_id,
            "date": o.date,
            "entry_time": o.entry_time,
            "exit_time": o.exit_time,
            "source": o.source,
        }
        for o in occupancies_orm
    ]

    # ------------------------------------------------------------------
    # Fetch resources (availability status for constraint checking)
    # ------------------------------------------------------------------
    resources_orm = db.query(models.ResourceModel).all()
    resources_input: list[dict] = [
        {
            "id": r.id,
            "resource_code": r.resource_code,
            "availability_status": r.availability_status,
        }
        for r in resources_orm
    ]

    # ------------------------------------------------------------------
    # Parse incompatible pairs from request body
    # ------------------------------------------------------------------
    incompatible_pairs: set[tuple[str, str]] = {
        (pair[0].upper(), pair[1].upper())
        for pair in incompatible_pairs_raw
        if len(pair) == 2
    }

    # ------------------------------------------------------------------
    # Run CP-SAT optimizer
    # ------------------------------------------------------------------
    result = run_cp_sat_block_planning(
        tasks=tasks_input,
        windows=windows_input,
        train_occupancies=occupancies_input,
        incompatible_pairs=incompatible_pairs,
        resources=resources_input,
        today=today,
        horizon_str=horizon,
    )

    # ------------------------------------------------------------------
    # Persist scheduled entries and update task statuses
    # ------------------------------------------------------------------
    created_at = datetime.utcnow()
    task_orm_by_id: dict[int, models.MaintenanceTaskModel] = {
        t.id: t for t in pending_tasks_orm
    }

    for entry in result["scheduled"]:
        task_orm = task_orm_by_id[entry["task_id"]]

        # Prevent duplicate block plan for the same task (defensive guard)
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
        )

    # ------------------------------------------------------------------
    # Build and return the API response
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
            "asset_availability_score": summary["asset_availability_score"],
            "coordinated_tasks": summary["coordinated_tasks"],
            "solve_time_ms": summary["solve_time_ms"],
        },
        "plan": [
            {
                "task_ref": e["task_ref"],
                "department": e["department_code"],
                "corridor_block_id": e["corridor_block_id"],
                "defect_type": e["defect_type"],
                "block_group_id": e["block_group_id"],
                "scheduled_date": str(e["scheduled_date"]),
                "entry_time": e["entry_time"],
                "exit_time": e["exit_time"],
                "priority_score": round(e["priority_score"], 2),
                "status": e["status"],
            }
            for e in result["scheduled"]
        ],
        "unscheduled": [
            {
                "task_ref": u["task_ref"],
                "department": u["department_code"],
                "corridor_block_id": u["corridor_block_id"],
                "defect_type": u["defect_type"],
                "criticality": u["criticality"],
                "due_date": str(u["due_date"]),
                "priority_score": round(u["priority_score"], 2),
                "reason": u["reason"],
            }
            for u in result["unscheduled"]
        ],
    }


def _reset_horizon_plans(horizon: str, db: Session) -> None:
    """Delete block plans for a horizon and reset task statuses to pending."""
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
        ).update({"status": "pending"}, synchronize_session=False)

    db.commit()


def reset_all_plans(db: Session) -> dict:
    """Reset ALL block plans (all horizons) — used by POST /block-plan/reset."""
    scheduled_task_ids = [
        row[0] for row in db.query(models.BlockPlanModel.task_id).distinct()
    ]

    db.query(models.BlockPlanModel).delete(synchronize_session=False)

    if scheduled_task_ids:
        db.query(models.MaintenanceTaskModel).filter(
            models.MaintenanceTaskModel.id.in_(scheduled_task_ids)
        ).update({"status": "pending"}, synchronize_session=False)

    db.commit()
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
            "asset_availability_score": 100.0,
            "coordinated_tasks": 0,
            "solve_time_ms": 0.0,
        },
        "plan": [],
        "unscheduled": [],
    }
