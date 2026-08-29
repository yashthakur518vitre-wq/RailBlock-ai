"""
services/scheduling_service.py — Block plan orchestration service.

Sits between the API layer and the CP-SAT optimizer:

  1. Fetch and prepare database data.
  2. Include train occupancy.
  3. Include already scheduled plans from other horizons.
  4. Call the CP-SAT optimizer.
  5. Validate the returned schedule defensively.
  6. Persist the optimized block plan.
  7. Update task statuses.
  8. Build the API response.

Important:
  Train occupancy and existing block plans are treated as hard occupancy
  constraints.

  Overnight occupancy is supported by including the previous calendar day
  when fetching occupancy records. The optimizer then splits overnight
  intervals internally.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from optimizer.cp_sat_optimizer import run_cp_sat_block_planning
from services.priority_service import compute_priority_score


# ===========================================================================
# DEBUG LOGGING
# ===========================================================================
#
# Controlled by the RAILBLOCK_DEBUG environment variable (default: on).
# Set RAILBLOCK_DEBUG=0 to silence these once the endpoint is confirmed
# to be working reliably.
# ===========================================================================

_DEBUG_ENABLED = os.getenv("RAILBLOCK_DEBUG", "1") != "0"


def _log(message: str) -> None:
    if _DEBUG_ENABLED:
        print(f"[GENERATE] {message}", flush=True, file=sys.stderr)


# ===========================================================================
# CONSTANTS
# ===========================================================================

HORIZON_DAYS = {
    "weekly": 7,
    "monthly": 30,
}

SOLVER_ENGINE = "Google OR-Tools CP-SAT"

# Because task duration is restricted to <= 24 hours, an occupancy starting
# one calendar day before the horizon can still overlap the first day.
OCCUPANCY_LOOKBACK_DAYS = 1


# ===========================================================================
# HELPERS
# ===========================================================================

def _optional_attr(instance, name: str, default=None):
    """Read optional schema fields without breaking older DB models."""
    return getattr(instance, name, default)


def _parse_time_to_minutes(value: str) -> int:
    """Convert HH:MM to minutes from midnight."""
    if not value:
        raise ValueError("Time cannot be empty.")

    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time '{value}'. Expected HH:MM.")

    hours, minutes = map(int, parts)

    if not 0 <= hours <= 23:
        raise ValueError(f"Invalid hour in '{value}'.")

    if not 0 <= minutes <= 59:
        raise ValueError(f"Invalid minute in '{value}'.")

    return hours * 60 + minutes


def _interval_overlaps(
    start_a: int,
    end_a: int,
    start_b: int,
    end_b: int,
) -> bool:
    """
    Half-open interval overlap test.

    Handles ordinary and overnight intervals by converting an overnight
    interval into an end time greater than 1440.
    """
    if end_a <= start_a:
        end_a += 1440

    if end_b <= start_b:
        end_b += 1440

    return start_a < end_b and start_b < end_a


def _validate_scheduled_entry(
    entry: dict,
    task_by_id: dict[int, object],
    window_by_id: dict[int, object],
) -> None:
    """
    Defensive validation of an optimizer result before database persistence.
    """

    task_id = entry.get("task_id")
    window_id = entry.get("window_id")

    task = task_by_id.get(task_id)
    window = window_by_id.get(window_id)

    if task is None:
        raise HTTPException(
            status_code=500,
            detail=f"Optimizer returned unknown task_id {task_id}.",
        )

    if window is None:
        raise HTTPException(
            status_code=500,
            detail=f"Optimizer returned unknown window_id {window_id}.",
        )

    if entry.get("corridor_id") != task.corridor_id:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Optimizer returned corridor mismatch for task {task_id}."
            ),
        )

    if entry.get("corridor_id") != window.corridor_id:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Optimizer scheduled task {task_id} "
                f"outside its corridor availability window."
            ),
        )

    scheduled_date = entry.get("scheduled_date")

    if scheduled_date != window.date:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Optimizer returned date mismatch for task {task_id} "
                f"and availability window {window_id}."
            ),
        )

    try:
        entry_start = _parse_time_to_minutes(entry["entry_time"])
        entry_end = _parse_time_to_minutes(entry["exit_time"])
        window_start = _parse_time_to_minutes(window.start_time)
        window_end = _parse_time_to_minutes(window.end_time)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Invalid time returned by optimizer for task {task_id}: "
                f"{exc}"
            ),
        ) from exc

    window_is_overnight = window_end <= window_start

    if window_is_overnight:
        window_end += 1440

    if entry_end <= entry_start:
        entry_end += 1440

    # ------------------------------------------------------------------
    # Overnight-window fix:
    #
    # An overnight window (e.g. 23:00 -> 04:00) is stored as a single
    # local-time range that wraps past midnight. A task can be placed
    # either in the "evening" portion (entry_start >= window_start,
    # e.g. 23:00-23:30) or in the "after-midnight" portion (e.g.
    # 03:00-04:00). The optimizer always reports entry/exit as plain
    # local wall-clock strings and keeps scheduled_date pinned to the
    # window's start date (matching the convention already used for
    # train occupancy).
    #
    # Because "03:00-04:00" does not itself cross midnight, the old
    # code compared it directly against the window's wrapped range
    # (1380 -> 1680) and incorrectly rejected it (180 < 1380). If the
    # window is overnight and the entry's start time is earlier than
    # the window's local start time, the entry must belong to the
    # after-midnight portion of the SAME window occurrence, so it is
    # shifted onto the same absolute timeline before comparing.
    # ------------------------------------------------------------------
    if window_is_overnight and entry_start < window_start:
        entry_start += 1440
        entry_end += 1440

    if entry_start < window_start or entry_end > window_end:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Optimizer returned task {task_id} outside availability "
                f"window {window_id}."
            ),
        )

    duration = int(task.estimated_duration_minutes)

    if entry_end - entry_start != duration:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Optimizer returned incorrect duration for task {task_id}. "
                f"Expected {duration} minutes."
            ),
        )


# ===========================================================================
# MAIN SERVICE
# ===========================================================================

def generate_block_plan(
    horizon: str,
    db: Session,
    incompatible_pairs_raw: list[list[str]],
    regenerate: bool,
) -> dict:
    """Generate, persist, and return a railway maintenance block plan."""

    _request_start = time.time()
    _log(f"Starting (horizon={horizon!r}, regenerate={regenerate!r})")

    # ------------------------------------------------------------------
    # Validate horizon
    # ------------------------------------------------------------------

    if horizon not in HORIZON_DAYS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid horizon '{horizon}'. "
                "Must be 'weekly' or 'monthly'."
            ),
        )

    today = date.today()
    horizon_days = HORIZON_DAYS[horizon]
    horizon_end = today + timedelta(days=horizon_days - 1)

    occupancy_start = today - timedelta(days=OCCUPANCY_LOOKBACK_DAYS)

    # ------------------------------------------------------------------
    # Existing plans for THIS horizon
    # ------------------------------------------------------------------

    existing_count = (
        db.query(models.BlockPlanModel)
        .filter(
            models.BlockPlanModel.horizon == horizon,
        )
        .count()
    )

    _log(f"Existing block plan rows for horizon={horizon!r}: {existing_count}")

    if existing_count > 0:

        if not regenerate:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{existing_count} block plan entries already exist "
                    f"for horizon '{horizon}'. "
                    "Use ?regenerate=true to clear and regenerate."
                ),
            )

        _reset_horizon_plans(
            horizon=horizon,
            db=db,
        )

    # ------------------------------------------------------------------
    # Fetch pending tasks
    # ------------------------------------------------------------------

    pending_tasks_orm = (
        db.query(models.MaintenanceTaskModel)
        .filter(
            models.MaintenanceTaskModel.status == "pending",
        )
        .all()
    )

    _log(f"Loaded pending tasks: {len(pending_tasks_orm)}")

    if not pending_tasks_orm:

        _log("No pending tasks - returning empty response.")

        try:
            db.commit()

        except Exception as exc:
            db.rollback()

            raise HTTPException(
                status_code=500,
                detail=(
                    "Database error committing empty block-plan reset: "
                    f"{exc}"
                ),
            ) from exc

        return _build_empty_response(
            horizon=horizon,
            start_date=today,
            end_date=horizon_end,
            solver_status="No pending tasks found.",
        )

    # ------------------------------------------------------------------
    # Prepare task input
    # ------------------------------------------------------------------

    tasks_input: list[dict] = []

    _log(
        "Computing priority scores for "
        f"{len(pending_tasks_orm)} task(s) "
        "(entering services.priority_service.compute_priority_score)..."
    )

    for _task_seq, task in enumerate(pending_tasks_orm, start=1):

        _log(
            f"  priority_score[{_task_seq}/{len(pending_tasks_orm)}] "
            f"task_id={task.id} task_ref={task.task_ref!r} - calling "
            "compute_priority_score()"
        )

        score = compute_priority_score(
            criticality=task.criticality,
            due_date=task.due_date,
            asset_impact_score=task.asset_impact_score,
            as_of=today,
        )

        _log(
            f"  priority_score[{_task_seq}/{len(pending_tasks_orm)}] "
            f"task_id={task.id} -> score={score}"
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
                "estimated_duration_minutes": (
                    task.estimated_duration_minutes
                ),
                "priority_score": score,
                "required_resource_id": task.required_resource_id,
                "due_date": task.due_date,
                "criticality": task.criticality,
                "asset_impact_score": task.asset_impact_score,
            }
        )

    tasks_input.sort(
        key=lambda item: item["priority_score"],
        reverse=True,
    )

    _log(f"Priority scoring complete. tasks_input={len(tasks_input)}")

    # ------------------------------------------------------------------
    # Availability windows
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

    _log(
        f"Loaded availability windows: {len(windows_orm)} "
        f"(range {today} .. {horizon_end})"
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
                "is_goods_forecast_clear": (
                    window.is_goods_forecast_clear
                ),
                "expected_goods_trains": _optional_attr(
                    window,
                    "expected_goods_trains",
                    None,
                ),
                "freight_probability": _optional_attr(
                    window,
                    "freight_probability",
                    None,
                ),
                "forecast_confidence": _optional_attr(
                    window,
                    "forecast_confidence",
                    None,
                ),
            }
        )

    # Remove unsupported optional values.
    for window in windows_input:

        for key in (
            "expected_goods_trains",
            "freight_probability",
            "forecast_confidence",
        ):

            if window.get(key) is None:
                window.pop(key, None)

    # ------------------------------------------------------------------
    # Train occupancies
    # ------------------------------------------------------------------
    #
    # IMPORTANT:
    # We intentionally query one day before the horizon.
    #
    # Example:
    #
    # Aug 27 23:30 -> Aug 28 01:30
    #
    # When generating Aug 28 onward, the Aug 27 record must still be
    # visible because it occupies the corridor on Aug 28.
    # ------------------------------------------------------------------

    occupancies_orm = (
        db.query(models.TrainOccupancyModel)
        .filter(
            models.TrainOccupancyModel.date >= occupancy_start,
            models.TrainOccupancyModel.date <= horizon_end,
        )
        .all()
    )

    _log(f"Loaded train occupancies: {len(occupancies_orm)}")

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

    # ------------------------------------------------------------------
    # Cross-horizon block-plan occupancy
    # ------------------------------------------------------------------
    #
    # Existing plans from another horizon are hard occupancy.
    #
    # Example:
    #
    # weekly:
    #   Aug 27 23:00 -> Aug 28 01:00
    #
    # monthly:
    #   cannot schedule over Aug 28 00:00 -> 01:00
    #
    # We query one day before the horizon for the same reason as train
    # occupancy.
    # ------------------------------------------------------------------

    other_horizon_plans_orm = (
        db.query(models.BlockPlanModel)
        .filter(
            models.BlockPlanModel.horizon != horizon,
            models.BlockPlanModel.scheduled_date >= occupancy_start,
            models.BlockPlanModel.scheduled_date <= horizon_end,
        )
        .all()
    )

    for plan in other_horizon_plans_orm:

        occupancies_input.append(
            {
                "corridor_id": plan.corridor_id,
                "date": plan.scheduled_date,
                "entry_time": plan.entry_time,
                "exit_time": plan.exit_time,
                "source": (
                    f"existing_block_plan:{plan.horizon}"
                ),
            }
        )

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    resources_orm = (
        db.query(models.ResourceModel)
        .all()
    )

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

    corridors_orm = (
        db.query(models.CorridorModel)
        .all()
    )

    corridor_capacities = {
        corridor.id: max(
            1,
            int(corridor.line_capacity_per_day or 1),
        )
        for corridor in corridors_orm
    }

    # ------------------------------------------------------------------
    # Safety incompatibilities
    # ------------------------------------------------------------------

    incompatible_pairs: set[tuple[str, str]] = {
        (
            str(pair[0]).upper(),
            str(pair[1]).upper(),
        )
        for pair in incompatible_pairs_raw
        if len(pair) == 2
    }

    # ------------------------------------------------------------------
    # Run CP-SAT
    # ------------------------------------------------------------------

    _log(
        "Loaded resources="
        f"{len(resources_input)}, corridors={len(corridor_capacities)}, "
        f"occupancy_records={len(occupancies_input)}. "
        "Calling optimizer.cp_sat_optimizer.run_cp_sat_block_planning()..."
    )

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

    except HTTPException:
        _log("Optimizer raised HTTPException - rolling back.")
        db.rollback()
        raise

    except Exception as exc:
        _log(f"Optimizer raised unexpected exception: {exc!r} - rolling back.")
        db.rollback()
        raise

    _log(
        "Optimizer returned. "
        f"scheduled={len(result.get('scheduled', []))}, "
        f"unscheduled={len(result.get('unscheduled', []))}, "
        f"solver_status={result.get('optimization_summary', {}).get('solver_status')}"
    )

    # ------------------------------------------------------------------
    # Defensive result validation
    # ------------------------------------------------------------------

    task_orm_by_id = {
        task.id: task
        for task in pending_tasks_orm
    }

    window_orm_by_id = {
        window.id: window
        for window in windows_orm
    }

    for entry in result["scheduled"]:

        _validate_scheduled_entry(
            entry=entry,
            task_by_id=task_orm_by_id,
            window_by_id=window_orm_by_id,
        )

    _log("Defensive validation of scheduled entries passed. Saving result...")

    # ------------------------------------------------------------------
    # Persist scheduled entries
    # ------------------------------------------------------------------

    created_at = datetime.utcnow()

    for entry in result["scheduled"]:

        task_orm = task_orm_by_id.get(
            entry["task_id"]
        )

        if task_orm is None:

            db.rollback()

            raise HTTPException(
                status_code=500,
                detail=(
                    "Optimizer returned unknown task_id "
                    f"{entry['task_id']}."
                ),
            )

        # Defensive guard.
        existing_plan = (
            db.query(models.BlockPlanModel)
            .filter(
                models.BlockPlanModel.task_id == task_orm.id,
            )
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

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    try:

        db.commit()

    except Exception as exc:

        db.rollback()

        raise HTTPException(
            status_code=500,
            detail=(
                "Database error while persisting block plan: "
                f"{exc}"
            ),
        ) from exc

    # ------------------------------------------------------------------
    # API response
    # ------------------------------------------------------------------

    summary = result["optimization_summary"]

    _log(
        f"Completed in {(time.time() - _request_start) * 1000:.1f} ms "
        f"(solver_status={summary.get('solver_status')}, "
        f"scheduled={len(result['scheduled'])}, "
        f"unscheduled={len(result['unscheduled'])})"
    )

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
            "total_blocks": summary.get(
                "total_blocks",
                0,
            ),
            "asset_availability_score": summary[
                "asset_availability_score"
            ],
            "coordinated_tasks": summary[
                "coordinated_tasks"
            ],
            "shared_blocks": summary.get(
                "shared_blocks",
                0,
            ),
            "overdue_tasks_scheduled": summary.get(
                "overdue_tasks_scheduled",
                0,
            ),
            "average_scheduled_priority": summary.get(
                "average_scheduled_priority",
                0.0,
            ),
            "solve_time_ms": summary[
                "solve_time_ms"
            ],
        },
        "plan": [
            {
                "task_ref": entry["task_ref"],
                "department": entry["department_code"],
                "corridor_block_id": entry[
                    "corridor_block_id"
                ],
                "defect_type": entry[
                    "defect_type"
                ],
                "block_group_id": entry[
                    "block_group_id"
                ],
                "scheduled_date": str(
                    entry["scheduled_date"]
                ),
                "entry_time": entry[
                    "entry_time"
                ],
                "exit_time": entry[
                    "exit_time"
                ],
                "priority_score": round(
                    entry["priority_score"],
                    2,
                ),
                "status": entry["status"],
            }
            for entry in result["scheduled"]
        ],
        "unscheduled": [
            {
                "task_ref": item["task_ref"],
                "department": item[
                    "department_code"
                ],
                "corridor_block_id": item[
                    "corridor_block_id"
                ],
                "defect_type": item[
                    "defect_type"
                ],
                "criticality": item[
                    "criticality"
                ],
                "due_date": str(
                    item["due_date"]
                ),
                "priority_score": round(
                    item["priority_score"],
                    2,
                ),
                "reason": item["reason"],
            }
            for item in result["unscheduled"]
        ],
    }


# ===========================================================================
# RESET CURRENT HORIZON
# ===========================================================================

def _reset_horizon_plans(
    horizon: str,
    db: Session,
) -> None:
    """
    Delete block plans for one horizon and reset affected tasks.

    No commit is performed here.
    """

    scheduled_task_ids = [
        row[0]
        for row in (
            db.query(
                models.BlockPlanModel.task_id
            )
            .filter(
                models.BlockPlanModel.horizon
                == horizon
            )
            .distinct()
        )
    ]

    db.query(
        models.BlockPlanModel
    ).filter(
        models.BlockPlanModel.horizon == horizon
    ).delete(
        synchronize_session=False
    )

    if scheduled_task_ids:

        db.query(
            models.MaintenanceTaskModel
        ).filter(
            models.MaintenanceTaskModel.id.in_(
                scheduled_task_ids
            )
        ).update(
            {
                "status": "pending"
            },
            synchronize_session=False,
        )


# ===========================================================================
# RESET EVERYTHING
# ===========================================================================

def reset_all_plans(
    db: Session,
) -> dict:
    """Reset ALL block plans and affected tasks."""

    scheduled_task_ids = [
        row[0]
        for row in (
            db.query(
                models.BlockPlanModel.task_id
            ).distinct()
        )
    ]

    db.query(
        models.BlockPlanModel
    ).delete(
        synchronize_session=False
    )

    if scheduled_task_ids:

        db.query(
            models.MaintenanceTaskModel
        ).filter(
            models.MaintenanceTaskModel.id.in_(
                scheduled_task_ids
            )
        ).update(
            {
                "status": "pending"
            },
            synchronize_session=False,
        )

    try:

        db.commit()

    except Exception as exc:

        db.rollback()

        raise HTTPException(
            status_code=500,
            detail=(
                "Database error during block plan reset: "
                f"{exc}"
            ),
        ) from exc

    return {
        "message": (
            f"Reset {len(scheduled_task_ids)} task(s) "
            "back to pending, cleared all block plan entries."
        ),
        "tasks_reset": len(scheduled_task_ids),
    }


# ===========================================================================
# EMPTY RESPONSE
# ===========================================================================

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