"""
optimizer/cp_sat_optimizer.py

RailBlock AI — Integrated Railway Maintenance Block Optimizer

Purpose
-------
Generate optimized railway maintenance block plans using Google OR-Tools
CP-SAT.

The optimizer combines:

    Maintenance tasks
        +
    Corridor availability
        +
    Timetable/train occupancy
        +
    Freight/goods forecast
        +
    Maintenance resources
        +
    Safety compatibility
        +
    Cross-department coordination

Important
---------
This is a decision-support optimizer, not a railway safety authority.

Hard operational/safety constraints are enforced by the model.
Soft business objectives are optimized by CP-SAT.

The production system should retain human approval before final
block authorization.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Optional, Any

from ortools.sat.python import cp_model


# ============================================================================
# CONSTANTS
# ============================================================================

SOLVER_TIME_LIMIT_SECONDS = 60.0
SOLVER_WORKERS = 8

# CP-SAT requires integer coefficients.
OBJECTIVE_SCALE = 100

# ---------------------------------------------------------------------------
# Objective weights
#
# These are intentionally configurable.
#
# Higher value = stronger influence on the optimizer.
# ---------------------------------------------------------------------------

WEIGHT_PRIORITY = 1000
WEIGHT_CRITICALITY = 500
WEIGHT_ASSET_IMPACT = 300
WEIGHT_OVERDUE = 250

WEIGHT_COORDINATION = 150

PENALTY_BLOCK_COUNT = 120
PENALTY_BLOCK_MINUTE = 2

PENALTY_TRAIN_IMPACT = 1000
PENALTY_FREIGHT_IMPACT = 250

PENALTY_LATE_DAY = 200


# ============================================================================
# TIME HELPERS
# ============================================================================

def _to_minutes(time_str: str) -> int:
    """
    Convert HH:MM to minutes from midnight.

    Example:
        02:30 -> 150
    """
    if not time_str:
        raise ValueError("Time cannot be empty.")

    parts = time_str.strip().split(":")

    if len(parts) != 2:
        raise ValueError(
            f"Invalid time '{time_str}'. Expected HH:MM."
        )

    hours, minutes = map(int, parts)

    if not 0 <= hours <= 23:
        raise ValueError(f"Invalid hour in '{time_str}'.")

    if not 0 <= minutes <= 59:
        raise ValueError(f"Invalid minute in '{time_str}'.")

    return hours * 60 + minutes


def _from_minutes(total_minutes: int) -> str:
    """
    Convert minutes from midnight to HH:MM.

    Values beyond 24 hours are wrapped for display.
    """
    total_minutes = total_minutes % (24 * 60)

    hours = total_minutes // 60
    minutes = total_minutes % 60

    return f"{hours:02d}:{minutes:02d}"


def _overlaps(
    start_a: int,
    end_a: int,
    start_b: int,
    end_b: int,
) -> bool:
    """
    Return True when two half-open intervals overlap.

    [start_a, end_a)
    [start_b, end_b)
    """
    return start_a < end_b and start_b < end_a


def _duration(start: int, end: int) -> int:
    """Return interval duration in minutes."""
    return max(0, end - start)


# ============================================================================
# FREE INTERVAL CALCULATION
# ============================================================================

def _compute_free_intervals(
    window_start: int,
    window_end: int,
    occupancy_ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """
    Remove train occupancy intervals from a maintenance availability window.

    Example:

        Window:
        01:00 ---------------- 05:00

        Train:
              02:00 -- 02:30

        Result:
        01:00 -- 02:00
        02:30 -- 05:00
    """

    if window_end <= window_start:
        return []

    clipped: list[tuple[int, int]] = []

    for start, end in occupancy_ranges:

        if end <= start:
            continue

        overlap_start = max(start, window_start)
        overlap_end = min(end, window_end)

        if overlap_start < overlap_end:
            clipped.append(
                (overlap_start, overlap_end)
            )

    if not clipped:
        return [(window_start, window_end)]

    clipped.sort()

    # Merge overlapping occupancy ranges.
    merged: list[tuple[int, int]] = []

    current_start, current_end = clipped[0]

    for start, end in clipped[1:]:

        if start <= current_end:
            current_end = max(current_end, end)

        else:
            merged.append(
                (current_start, current_end)
            )

            current_start = start
            current_end = end

    merged.append(
        (current_start, current_end)
    )

    # Calculate free intervals.
    free: list[tuple[int, int]] = []

    cursor = window_start

    for start, end in merged:

        if cursor < start:
            free.append(
                (cursor, start)
            )

        cursor = max(cursor, end)

    if cursor < window_end:
        free.append(
            (cursor, window_end)
        )

    return free


def _find_fitting_starts(
    free_intervals: list[tuple[int, int]],
    duration_minutes: int,
) -> list[int]:
    """
    Return all feasible start positions for a task.

    We don't enumerate every minute because that can create a very large
    CP-SAT model.

    Instead, we expose useful candidate positions:
        - beginning of the free interval
        - end-aligned position

    The optimizer can then choose between meaningful alternatives.
    """

    starts: list[int] = []

    if duration_minutes <= 0:
        return starts

    for start, end in free_intervals:

        if end - start < duration_minutes:
            continue

        starts.append(start)

        end_aligned_start = end - duration_minutes

        if end_aligned_start != start:
            starts.append(end_aligned_start)

    return sorted(set(starts))


# ============================================================================
# NORMALIZATION HELPERS
# ============================================================================

def _criticality_score(value: Any) -> int:
    """
    Convert railway criticality into a positive optimization score.

    Existing model:
        1 = highest criticality
        5 = lowest criticality

    Therefore:
        1 -> 5
        5 -> 1
    """

    try:
        criticality = int(value)
    except (TypeError, ValueError):
        criticality = 5

    criticality = max(1, min(5, criticality))

    return 6 - criticality


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _overdue_days(
    due_date: Any,
    today: date,
) -> int:
    """
    Calculate number of days a task is overdue.

    Future due date -> 0
    Due today       -> 0
    Past due date   -> positive number
    """

    if not due_date:
        return 0

    try:
        delta = today - due_date
        return max(0, delta.days)
    except Exception:
        return 0


def _freight_penalty(window: dict) -> int:
    """
    Convert freight forecast information into a penalty.

    Backward compatibility:
        is_goods_forecast_clear=True  -> 0 penalty
        is_goods_forecast_clear=False -> configured penalty

    New recommended fields:
        expected_goods_trains
        freight_probability
        forecast_confidence

    The optimizer supports both models.
    """

    expected_goods = _safe_float(
        window.get("expected_goods_trains"),
        0.0,
    )

    probability = _safe_float(
        window.get("freight_probability"),
        0.0,
    )

    # Backward-compatible binary field.
    if (
        "expected_goods_trains" not in window
        and "freight_probability" not in window
    ):
        if window.get("is_goods_forecast_clear", True) is False:
            return PENALTY_FREIGHT_IMPACT

        return 0

    penalty = (
        expected_goods * 20.0
        + probability * 100.0
    )

    return int(round(penalty))


# ============================================================================
# MAIN OPTIMIZER
# ============================================================================

def run_cp_sat_block_planning(
    tasks: list[dict],
    windows: list[dict],
    train_occupancies: list[dict],
    incompatible_pairs: set[tuple[str, str]],
    resources: list[dict],
    today: date,
    horizon_str: str,
) -> dict:
    """
    Generate an optimized railway maintenance block plan.

    Decision:
        Which task gets which start time/window?

    Hard constraints:
        1. A task can be scheduled at most once.
        2. Task must remain inside its corridor availability window.
        3. Task must not overlap train occupancy.
        4. Required resource must be available.
        5. Same resource cannot overlap temporally.
        6. Safety-incompatible tasks cannot share a block.
        7. Tasks from the same department may coexist if their resources
           and safety constraints permit it.

    Soft objectives:
        + Complete high priority tasks.
        + Complete critical tasks.
        + Restore high-impact assets.
        + Complete overdue tasks.
        + Coordinate multiple departments.
        - Use too many separate blocks.
        - Consume excessive block minutes.
        - Use high-freight-impact windows.
        - Delay tasks beyond their due dates.

    Returns:
        scheduled
        unscheduled
        optimization_summary
    """

    solve_start = time.time()

    if not tasks:
        return _empty_result(
            "no_tasks",
            horizon_str,
            solve_start,
        )

    if not windows:
        return {
            "scheduled": [],
            "unscheduled": [
                {
                    "task_id": task.get("id"),
                    "task_ref": task.get("task_ref"),
                    "department_code": task.get("department_code"),
                    "corridor_block_id": task.get("corridor_block_id"),
                    "defect_type": task.get("defect_type"),
                    "criticality": task.get("criticality"),
                    "due_date": task.get("due_date"),
                    "priority_score": task.get("priority_score", 0),
                    "reason": "no_availability_window",
                }
                for task in tasks
            ],
            "optimization_summary": {
                "solver_status": "NO_WINDOWS",
                "total_scheduled": 0,
                "total_unscheduled": len(tasks),
                "total_block_minutes": 0,
                "asset_availability_score": 100.0,
                "coordinated_tasks": 0,
                "shared_blocks": 0,
                "solve_time_ms": round(
                    (time.time() - solve_start) * 1000,
                    2,
                ),
            },
        }

    # ========================================================================
    # STEP 1 — TRAIN OCCUPANCY INDEX
    # ========================================================================

    occupancy_lookup: dict[
        tuple[int, date],
        list[tuple[int, int]],
    ] = {}

    for occupancy in train_occupancies:

        corridor_id = occupancy["corridor_id"]
        occupancy_date = occupancy["date"]

        try:
            start = _to_minutes(
                occupancy["entry_time"]
            )

            end = _to_minutes(
                occupancy["exit_time"]
            )

        except (ValueError, TypeError):
            continue

        # Handle overnight train occupancy.
        if end <= start:
            end += 24 * 60

        key = (
            corridor_id,
            occupancy_date,
        )

        occupancy_lookup.setdefault(
            key,
            [],
        ).append(
            (start, end)
        )

    # ========================================================================
    # STEP 2 — PREPROCESS WINDOWS
    # ========================================================================

    window_meta: list[dict] = []

    for window in windows:

        try:
            start = _to_minutes(
                window["start_time"]
            )

            end = _to_minutes(
                window["end_time"]
            )

        except (ValueError, TypeError, KeyError):
            continue

        # Overnight window.
        if end <= start:
            end += 24 * 60

        occupancy_ranges = occupancy_lookup.get(
            (
                window["corridor_id"],
                window["date"],
            ),
            [],
        )

        free_intervals = _compute_free_intervals(
            start,
            end,
            occupancy_ranges,
        )

        total_free = sum(
            _duration(s, e)
            for s, e in free_intervals
        )

        window_meta.append(
            {
                **window,
                "w_start": start,
                "w_end": end,
                "free_intervals": free_intervals,
                "total_free_minutes": total_free,
                "freight_penalty": _freight_penalty(window),
            }
        )

    # ========================================================================
    # STEP 3 — RESOURCE AVAILABILITY
    # ========================================================================

    resource_available: dict[int, bool] = {
        resource["id"]: (
            resource.get("availability_status", "available")
            == "available"
        )
        for resource in resources
    }

    # ========================================================================
    # STEP 4 — NORMALIZE SAFETY RULES
    # ========================================================================

    normalized_incompatible: set[frozenset[str]] = set()

    for pair in incompatible_pairs:

        if len(pair) != 2:
            continue

        a = str(pair[0]).upper()
        b = str(pair[1]).upper()

        normalized_incompatible.add(
            frozenset((a, b))
        )

    # ========================================================================
    # STEP 5 — BUILD CANDIDATE START TIMES
    # ========================================================================

    #
    # candidate key:
    #
    #     (task_index, window_index, start_time)
    #
    # Each candidate is a possible actual placement.
    #

    candidates: dict[
        tuple[int, int, int],
        dict,
    ] = {}

    infeasibility_reasons: dict[
        int,
        set[str],
    ] = {
        i: set()
        for i in range(len(tasks))
    }

    for task_index, task in enumerate(tasks):

        duration = int(
            task.get(
                "estimated_duration_minutes",
                0,
            )
        )

        if duration <= 0:
            infeasibility_reasons[
                task_index
            ].add(
                "invalid_duration"
            )

            continue

        for window_index, window in enumerate(window_meta):

            # --------------------------------------------------------------
            # Corridor compatibility
            # --------------------------------------------------------------

            if (
                task["corridor_id"]
                != window["corridor_id"]
            ):
                continue

            # --------------------------------------------------------------
            # Resource availability
            # --------------------------------------------------------------

            resource_id = task.get(
                "required_resource_id"
            )

            if (
                resource_id is not None
                and not resource_available.get(
                    resource_id,
                    False,
                )
            ):
                infeasibility_reasons[
                    task_index
                ].add(
                    "resource_unavailable"
                )

                continue

            # --------------------------------------------------------------
            # Candidate starts
            # --------------------------------------------------------------

            starts = _find_fitting_starts(
                window["free_intervals"],
                duration,
            )

            if not starts:

                infeasibility_reasons[
                    task_index
                ].add(
                    "insufficient_window_duration"
                )

                continue

            for start in starts:

                end = start + duration

                candidates[
                    (
                        task_index,
                        window_index,
                        start,
                    )
                ] = {
                    "task_index": task_index,
                    "window_index": window_index,
                    "start": start,
                    "end": end,
                }

    # ========================================================================
    # STEP 6 — CP-SAT MODEL
    # ========================================================================

    model = cp_model.CpModel()

    # Candidate -> BoolVar
    assign: dict[
        tuple[int, int, int],
        cp_model.IntVar,
    ] = {}

    for key in candidates:

        task_index, window_index, start = key

        assign[key] = model.new_bool_var(
            f"assign_t{task_index}_w{window_index}_s{start}"
        )

    # ========================================================================
    # CONSTRAINT 1 — TASK AT MOST ONCE
    # ========================================================================

    for task_index in range(len(tasks)):

        variables = [
            variable
            for (
                candidate_task,
                _,
                _,
            ), variable in assign.items()
            if candidate_task == task_index
        ]

        if variables:
            model.add(
                sum(variables) <= 1
            )

    # ========================================================================
    # CONSTRAINT 2 — RESOURCE OVERLAP
    # ========================================================================

    #
    # Important fix:
    #
    # We do NOT merely check whether two tasks belong to the same window.
    #
    # We compare their actual start/end times.
    #

    candidate_items = list(assign.items())

    for index_a in range(len(candidate_items)):

        key_a, var_a = candidate_items[index_a]

        task_a_idx, window_a_idx, start_a = key_a

        task_a = tasks[task_a_idx]

        resource_a = task_a.get(
            "required_resource_id"
        )

        if resource_a is None:
            continue

        duration_a = int(
            task_a["estimated_duration_minutes"]
        )

        end_a = start_a + duration_a

        for index_b in range(index_a + 1, len(candidate_items)):

            key_b, var_b = candidate_items[index_b]

            task_b_idx, window_b_idx, start_b = key_b

            task_b = tasks[task_b_idx]

            resource_b = task_b.get(
                "required_resource_id"
            )

            if resource_b != resource_a:
                continue

            # Same task is already controlled by Constraint 1.
            if task_a_idx == task_b_idx:
                continue

            # Resource conflicts only matter on the same date.
            date_a = window_meta[
                window_a_idx
            ]["date"]

            date_b = window_meta[
                window_b_idx
            ]["date"]

            if date_a != date_b:
                continue

            duration_b = int(
                task_b["estimated_duration_minutes"]
            )

            end_b = start_b + duration_b

            if _overlaps(
                start_a,
                end_a,
                start_b,
                end_b,
            ):
                model.add(
                    var_a + var_b <= 1
                )

    # ========================================================================
    # CONSTRAINT 3 — SAFETY INCOMPATIBILITY
    # ========================================================================

    for index_a in range(len(candidate_items)):

        key_a, var_a = candidate_items[index_a]

        task_a_idx, window_a_idx, start_a = key_a

        task_a = tasks[task_a_idx]

        for index_b in range(index_a + 1, len(candidate_items)):

            key_b, var_b = candidate_items[index_b]

            task_b_idx, window_b_idx, start_b = key_b

            task_b = tasks[task_b_idx]

            # Same task handled by task-at-most-once.
            if task_a_idx == task_b_idx:
                continue

            # Must be same date.
            if (
                window_meta[window_a_idx]["date"]
                != window_meta[window_b_idx]["date"]
            ):
                continue

            # Must be same corridor.
            if (
                window_meta[window_a_idx]["corridor_id"]
                != window_meta[window_b_idx]["corridor_id"]
            ):
                continue

            defect_pair = frozenset(
                (
                    str(
                        task_a["defect_type"]
                    ).upper(),
                    str(
                        task_b["defect_type"]
                    ).upper(),
                )
            )

            if defect_pair not in normalized_incompatible:
                continue

            end_a = (
                start_a
                + int(
                    task_a[
                        "estimated_duration_minutes"
                    ]
                )
            )

            end_b = (
                start_b
                + int(
                    task_b[
                        "estimated_duration_minutes"
                    ]
                )
            )

            if _overlaps(
                start_a,
                end_a,
                start_b,
                end_b,
            ):
                model.add(
                    var_a + var_b <= 1
                )

    # ========================================================================
    # CONSTRAINT 4 — SAME DEPARTMENT
    # ========================================================================

    #
    # IMPORTANT:
    #
    # We intentionally DO NOT impose:
    #
    #     "one task per department per window"
    #
    # because that was unnecessarily restrictive.
    #
    # Multiple teams in the same department may be able to work in parallel.
    #
    # Resource constraints and safety constraints control whether this is
    # actually allowed.
    #

    # ========================================================================
    # BLOCK GROUP VARIABLES
    # ========================================================================

    #
    # A block group represents one actual possession.
    #
    # We derive grouping from tasks that are assigned to the same:
    #
    #     corridor + date + overlapping time region
    #
    # For CP-SAT, we use pairwise coordination variables.
    #

    coordination_vars: list[
        tuple[cp_model.IntVar, int]
    ] = []

    for index_a in range(len(candidate_items)):

        key_a, var_a = candidate_items[index_a]

        task_a_idx, window_a_idx, start_a = key_a

        task_a = tasks[task_a_idx]

        end_a = (
            start_a
            + int(
                task_a[
                    "estimated_duration_minutes"
                ]
            )
        )

        for index_b in range(
            index_a + 1,
            len(candidate_items),
        ):

            key_b, var_b = candidate_items[index_b]

            task_b_idx, window_b_idx, start_b = key_b

            task_b = tasks[task_b_idx]

            if task_a_idx == task_b_idx:
                continue

            # Same date.
            if (
                window_meta[window_a_idx]["date"]
                != window_meta[window_b_idx]["date"]
            ):
                continue

            # Same corridor.
            if (
                window_meta[window_a_idx]["corridor_id"]
                != window_meta[window_b_idx]["corridor_id"]
            ):
                continue

            # Different departments are where coordination has value.
            if (
                task_a["department_id"]
                == task_b["department_id"]
            ):
                continue

            end_b = (
                start_b
                + int(
                    task_b[
                        "estimated_duration_minutes"
                    ]
                )
            )

            if not _overlaps(
                start_a,
                end_a,
                start_b,
                end_b,
            ):
                continue

            # Pairwise AND variable:
            #
            # coordination = var_a AND var_b
            #
            coord = model.new_bool_var(
                f"coord_t{task_a_idx}_t{task_b_idx}"
                f"_w{window_a_idx}_s{start_a}"
            )

            model.add(
                coord <= var_a
            )

            model.add(
                coord <= var_b
            )

            model.add(
                coord >= var_a + var_b - 1
            )

            coordination_vars.append(
                (
                    coord,
                    WEIGHT_COORDINATION,
                )
            )

    # ========================================================================
    # OBJECTIVE
    # ========================================================================

    objective_terms = []

    for key, variable in assign.items():

        task_index, window_index, start = key

        task = tasks[task_index]
        window = window_meta[window_index]

        # --------------------------------------------------------------
        # Priority
        # --------------------------------------------------------------

        priority_score = _safe_float(
            task.get("priority_score"),
            0.0,
        )

        priority_component = int(
            round(
                priority_score
                * WEIGHT_PRIORITY
            )
        )

        # --------------------------------------------------------------
        # Criticality
        # --------------------------------------------------------------

        criticality_component = (
            _criticality_score(
                task.get(
                    "criticality",
                    5,
                )
            )
            * WEIGHT_CRITICALITY
        )

        # --------------------------------------------------------------
        # Asset impact
        # --------------------------------------------------------------

        asset_impact = max(
            0.0,
            min(
                100.0,
                _safe_float(
                    task.get(
                        "asset_impact_score",
                        0,
                    )
                ),
            ),
        )

        asset_component = int(
            round(
                asset_impact
                * WEIGHT_ASSET_IMPACT
            )
        )

        # --------------------------------------------------------------
        # Overdue task bonus
        # --------------------------------------------------------------

        overdue = _overdue_days(
            task.get("due_date"),
            today,
        )

        overdue_component = (
            min(overdue, 30)
            * WEIGHT_OVERDUE
        )

        # --------------------------------------------------------------
        # Freight impact penalty
        # --------------------------------------------------------------

        freight_component = (
            _freight_penalty(window)
            * PENALTY_FREIGHT_IMPACT
        )

        # --------------------------------------------------------------
        # Block-duration penalty
        # --------------------------------------------------------------

        duration = int(
            task.get(
                "estimated_duration_minutes",
                0,
            )
        )

        duration_penalty = (
            duration
            * PENALTY_BLOCK_MINUTE
        )

        # --------------------------------------------------------------
        # Candidate score
        # --------------------------------------------------------------

        candidate_score = (
            priority_component
            + criticality_component
            + asset_component
            + overdue_component
            - freight_component
            - duration_penalty
        )

        objective_terms.append(
            candidate_score
            * variable
        )

    # --------------------------------------------------------------
    # Coordination bonus
    # --------------------------------------------------------------

    for variable, weight in coordination_vars:

        objective_terms.append(
            weight * variable
        )

    # --------------------------------------------------------------
    # Maximize objective
    # --------------------------------------------------------------

    if objective_terms:
        model.maximize(
            sum(objective_terms)
        )

    # ========================================================================
    # SOLVE
    # ========================================================================

    solver = cp_model.CpSolver()

    solver.parameters.max_time_in_seconds = (
        SOLVER_TIME_LIMIT_SECONDS
    )

    solver.parameters.num_search_workers = (
        SOLVER_WORKERS
    )

    solver.parameters.log_search_progress = False

    status = solver.solve(model)

    solve_ms = (
        time.time() - solve_start
    ) * 1000

    status_name = solver.status_name(
        status
    )

    # ========================================================================
    # EXTRACT SOLUTION
    # ========================================================================

    scheduled: list[dict] = []

    task_scheduled: dict[
        int,
        bool,
    ] = {
        i: False
        for i in range(len(tasks))
    }

    # Group selected assignments.
    selected_candidates: list[
        tuple[int, int, int]
    ] = []

    if status in (
        cp_model.OPTIMAL,
        cp_model.FEASIBLE,
    ):

        for key, variable in assign.items():

            if solver.boolean_value(variable):

                task_index, window_index, start = key

                task_scheduled[
                    task_index
                ] = True

                selected_candidates.append(
                    key
                )

    # ========================================================================
    # BUILD BLOCK GROUPS
    # ========================================================================

    #
    # A selected task is assigned to an actual date/time.
    #
    # We group tasks whose intervals overlap on the same corridor/date.
    #

    selected_candidates.sort(
        key=lambda key: (
            window_meta[key[1]]["date"],
            window_meta[key[1]]["corridor_id"],
            key[2],
        )
    )

    block_groups: list[list[
        tuple[int, int, int]
    ]] = []

    for candidate in selected_candidates:

        task_index, window_index, start = candidate

        task = tasks[task_index]

        end = (
            start
            + int(
                task[
                    "estimated_duration_minutes"
                ]
            )
        )

        corridor_id = (
            window_meta[
                window_index
            ]["corridor_id"]
        )

        candidate_date = (
            window_meta[
                window_index
            ]["date"]
        )

        placed = False

        for group in block_groups:

            first_task_index, first_window_index, first_start = group[0]

            group_corridor = (
                window_meta[
                    first_window_index
                ]["corridor_id"]
            )

            group_date = (
                window_meta[
                    first_window_index
                ]["date"]
            )

            if (
                group_corridor
                != corridor_id
                or group_date
                != candidate_date
            ):
                continue

            group_start = min(
                item[2]
                for item in group
            )

            group_end = max(
                item[2]
                + int(
                    tasks[item[0]][
                        "estimated_duration_minutes"
                    ]
                )
                for item in group
            )

            if _overlaps(
                start,
                end,
                group_start,
                group_end,
            ):

                group.append(
                    candidate
                )

                placed = True

                break

        if not placed:
            block_groups.append(
                [candidate]
            )

    # ========================================================================
    # CREATE SCHEDULED OUTPUT
    # ========================================================================

    block_group_lookup: dict[
        tuple[int, int, int],
        str,
    ] = {}

    total_block_minutes = 0

    coordinated_tasks = 0

    shared_blocks = 0

    for group_number, group in enumerate(
        block_groups,
        start=1,
    ):

        if not group:
            continue

        first_task_index, first_window_index, _ = group[0]

        first_window = window_meta[
            first_window_index
        ]

        group_start = min(
            candidate[2]
            for candidate in group
        )

        group_end = max(
            candidate[2]
            + int(
                tasks[candidate[0]][
                    "estimated_duration_minutes"
                ]
            )
            for candidate in group
        )

        group_duration = (
            group_end
            - group_start
        )

        total_block_minutes += (
            group_duration
        )

        departments = {
            tasks[candidate[0]][
                "department_id"
            ]
            for candidate in group
        }

        if len(departments) > 1:

            shared_blocks += 1

            coordinated_tasks += len(
                group
            )

        block_group_id = (
            f"BG{horizon_str[0].upper()}"
            f"-{first_window['id']}"
            f"-{group_start}"
            f"-{group_number}"
        )

        for candidate in group:

            block_group_lookup[
                candidate
            ] = block_group_id

    # ------------------------------------------------------------------------
    # Build task output.
    # ------------------------------------------------------------------------

    for candidate in selected_candidates:

        task_index, window_index, start = candidate

        task = tasks[task_index]

        duration = int(
            task[
                "estimated_duration_minutes"
            ]
        )

        end = start + duration

        block_group_id = (
            block_group_lookup[
                candidate
            ]
        )

        scheduled.append(
            {
                "task_id": task["id"],
                "task_ref": task["task_ref"],
                "department_id": task[
                    "department_id"
                ],
                "department_code": task[
                    "department_code"
                ],
                "corridor_id": task[
                    "corridor_id"
                ],
                "corridor_block_id": task[
                    "corridor_block_id"
                ],
                "defect_type": task[
                    "defect_type"
                ],
                "window_id": window_meta[
                    window_index
                ]["id"],
                "block_group_id": block_group_id,
                "scheduled_date": window_meta[
                    window_index
                ]["date"],
                "entry_time": _from_minutes(
                    start
                ),
                "exit_time": _from_minutes(
                    end
                ),
                "priority_score": task[
                    "priority_score"
                ],
                "criticality": task[
                    "criticality"
                ],
                "asset_impact_score": task[
                    "asset_impact_score"
                ],
                "resource_id": task.get(
                    "required_resource_id"
                ),
                "status": (
                    "OPTIMAL"
                    if status
                    == cp_model.OPTIMAL
                    else "FEASIBLE"
                ),
            }
        )

    # ========================================================================
    # UNSCHEDULED TASKS
    # ========================================================================

    unscheduled: list[dict] = []

    for task_index, task in enumerate(tasks):

        if task_scheduled[task_index]:
            continue

        reason = _diagnose_unscheduled_reason(
            task_index=task_index,
            task=task,
            tasks=tasks,
            window_meta=window_meta,
            candidates=candidates,
            selected_candidates=selected_candidates,
            normalized_incompatible=normalized_incompatible,
            resource_available=resource_available,
            today=today,
        )

        unscheduled.append(
            {
                "task_id": task["id"],
                "task_ref": task[
                    "task_ref"
                ],
                "department_code": task[
                    "department_code"
                ],
                "corridor_block_id": task[
                    "corridor_block_id"
                ],
                "defect_type": task[
                    "defect_type"
                ],
                "criticality": task[
                    "criticality"
                ],
                "due_date": task[
                    "due_date"
                ],
                "priority_score": task[
                    "priority_score"
                ],
                "reason": reason,
            }
        )

    # ========================================================================
    # KPI METRICS
    # ========================================================================

    total_window_minutes = sum(
        window["total_free_minutes"]
        for window in window_meta
    )

    if total_window_minutes > 0:

        utilization = (
            total_block_minutes
            / total_window_minutes
        )

        asset_availability_score = round(
            max(
                0.0,
                min(
                    100.0,
                    100.0
                    * (1.0 - utilization),
                ),
            ),
            2,
        )

    else:

        asset_availability_score = 100.0

    # Count distinct blocks.
    total_blocks = len(
        block_groups
    )

    # Calculate average task priority scheduled.
    if scheduled:

        average_scheduled_priority = round(
            sum(
                entry["priority_score"]
                for entry in scheduled
            )
            / len(scheduled),
            2,
        )

    else:

        average_scheduled_priority = 0.0

    # Calculate overdue tasks scheduled.
    overdue_tasks_scheduled = 0

    for candidate in selected_candidates:

        task_index, _, _ = candidate

        if _overdue_days(
            tasks[task_index].get(
                "due_date"
            ),
            today,
        ) > 0:

            overdue_tasks_scheduled += 1

    # ========================================================================
    # FINAL RESULT
    # ========================================================================

    return {
        "scheduled": scheduled,

        "unscheduled": unscheduled,

        "optimization_summary": {
            "solver_status": status_name,

            "solver_optimal": (
                status
                == cp_model.OPTIMAL
            ),

            "solver_feasible": (
                status
                in (
                    cp_model.OPTIMAL,
                    cp_model.FEASIBLE,
                )
            ),

            "total_scheduled": len(
                scheduled
            ),

            "total_unscheduled": len(
                unscheduled
            ),

            "total_blocks": total_blocks,

            "total_block_minutes": (
                total_block_minutes
            ),

            "asset_availability_score": (
                asset_availability_score
            ),

            "coordinated_tasks": (
                coordinated_tasks
            ),

            "shared_blocks": (
                shared_blocks
            ),

            "overdue_tasks_scheduled": (
                overdue_tasks_scheduled
            ),

            "average_scheduled_priority": (
                average_scheduled_priority
            ),

            "solve_time_ms": round(
                solve_ms,
                2,
            ),

            "objective": (
                "priority + criticality + asset impact "
                "+ overdue completion + cross-department "
                "coordination - block count - block duration "
                "- freight impact"
            ),
        },
    }


# ============================================================================
# UNSCHEDULED TASK DIAGNOSIS
# ============================================================================

def _diagnose_unscheduled_reason(
    task_index: int,
    task: dict,
    tasks: list[dict],
    window_meta: list[dict],
    candidates: dict,
    selected_candidates: list[tuple[int, int, int]],
    normalized_incompatible: set[frozenset[str]],
    resource_available: dict[int, bool],
    today: date,
) -> str:
    """
    Explain why a task was not scheduled.

    This is intentionally a human-readable diagnostic layer.
    It does not pretend to extract an exact logical proof from CP-SAT.
    """

    # ------------------------------------------------------------------------
    # Invalid duration
    # ------------------------------------------------------------------------

    duration = int(
        task.get(
            "estimated_duration_minutes",
            0,
        )
    )

    if duration <= 0:
        return "invalid_duration"

    # ------------------------------------------------------------------------
    # Resource unavailable
    # ------------------------------------------------------------------------

    resource_id = task.get(
        "required_resource_id"
    )

    if (
        resource_id is not None
        and not resource_available.get(
            resource_id,
            False,
        )
    ):
        return "resource_unavailable"

    # ------------------------------------------------------------------------
    # Candidate windows
    # ------------------------------------------------------------------------

    task_candidates = [
        candidate
        for candidate in candidates
        if candidate[0] == task_index
    ]

    if not task_candidates:
        return "no_feasible_time_window"

    # ------------------------------------------------------------------------
    # Compare against selected tasks.
    # ------------------------------------------------------------------------

    selected_by_task = {
        candidate[0]
        for candidate in selected_candidates
    }

    task_defect = str(
        task.get(
            "defect_type",
            "",
        )
    ).upper()

    for (
        _,
        window_index,
        start,
    ) in task_candidates:

        end = start + duration

        window = window_meta[
            window_index
        ]

        for selected in selected_candidates:

            other_index = selected[0]

            if other_index == task_index:
                continue

            if other_index not in selected_by_task:
                continue

            other_task = tasks[
                other_index
            ]

            other_window = window_meta[
                selected[1]
            ]

            if (
                other_window["date"]
                != window["date"]
            ):
                continue

            if (
                other_window["corridor_id"]
                != window["corridor_id"]
            ):
                continue

            other_start = selected[2]

            other_end = (
                other_start
                + int(
                    other_task[
                        "estimated_duration_minutes"
                    ]
                )
            )

            if not _overlaps(
                start,
                end,
                other_start,
                other_end,
            ):
                continue

            # Resource conflict.
            if (
                resource_id is not None
                and other_task.get(
                    "required_resource_id"
                )
                == resource_id
            ):
                return "resource_conflict"

            # Safety conflict.
            other_defect = str(
                other_task.get(
                    "defect_type",
                    "",
                )
            ).upper()

            if frozenset(
                (
                    task_defect,
                    other_defect,
                )
            ) in normalized_incompatible:

                return (
                    "safety_incompatibility"
                )

            # Higher priority task took the opportunity.
            if (
                _safe_float(
                    other_task.get(
                        "priority_score"
                    )
                )
                >
                _safe_float(
                    task.get(
                        "priority_score"
                    )
                )
            ):

                return (
                    "higher_priority_task_preempted"
                )

    # ------------------------------------------------------------------------
    # Overdue / lower priority
    # ------------------------------------------------------------------------

    if (
        _overdue_days(
            task.get("due_date"),
            today,
        )
        > 0
    ):
        return "optimizer_tradeoff_overdue_task"

    return "optimizer_tradeoff_lower_priority"


# ============================================================================
# EMPTY RESULT
# ============================================================================

def _empty_result(
    reason: str,
    horizon_str: str,
    solve_start: float,
) -> dict:

    return {
        "scheduled": [],

        "unscheduled": [],

        "optimization_summary": {
            "solver_status": reason,
            "solver_optimal": False,
            "solver_feasible": False,
            "total_scheduled": 0,
            "total_unscheduled": 0,
            "total_blocks": 0,
            "total_block_minutes": 0,
            "asset_availability_score": 100.0,
            "coordinated_tasks": 0,
            "shared_blocks": 0,
            "overdue_tasks_scheduled": 0,
            "average_scheduled_priority": 0.0,
            "solve_time_ms": round(
                (
                    time.time()
                    - solve_start
                )
                * 1000,
                2,
            ),
            "objective": (
                "no optimization performed"
            ),
        },
    }