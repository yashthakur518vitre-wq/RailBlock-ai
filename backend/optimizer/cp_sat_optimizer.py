"""
optimizer/cp_sat_optimizer.py

RailBlock AI — Railway Maintenance Block Optimizer

CP-SAT based optimizer for SIH26027.

Design goals:
- Keep the CP-SAT model small and deterministic.
- Schedule every task at most once.
- Respect maintenance availability windows.
- Respect train occupancy.
- Support overnight train occupancy.
- Support overnight maintenance windows.
- Respect resource availability.
- Prevent resource overlap.
- Prevent configured safety-incompatible overlaps.
- Respect corridor simultaneous-capacity limits.
- Prioritize critical, overdue and high-impact maintenance.
- Group overlapping tasks into physical block groups.
- Never allow a long-running optimization request to hang indefinitely.

This optimizer is a decision-support system.
Final railway operational authorization remains subject to
human approval and applicable railway safety procedures.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from ortools.sat.python import cp_model


# ============================================================================
# CONFIGURATION
# ============================================================================

SOLVER_TIME_LIMIT_SECONDS = 10.0
SOLVER_WORKERS = 1

MINUTES_PER_DAY = 24 * 60


# ============================================================================
# OBJECTIVE WEIGHTS
# ============================================================================

WEIGHT_PRIORITY = 1000
WEIGHT_CRITICALITY = 500
WEIGHT_ASSET_IMPACT = 300
WEIGHT_OVERDUE = 250

PENALTY_BLOCK_MINUTE = 2
PENALTY_FREIGHT_IMPACT = 250
PENALTY_LATE_DAY = 20


# ============================================================================
# TIME HELPERS
# ============================================================================

def _to_minutes(value: Any) -> int:
    """Convert HH:MM into minutes from midnight."""

    if value is None:
        raise ValueError("Time cannot be empty.")

    text = str(value).strip()
    parts = text.split(":")

    if len(parts) != 2:
        raise ValueError(
            f"Invalid time '{value}'. Expected HH:MM."
        )

    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError as exc:
        raise ValueError(
            f"Invalid time '{value}'. Expected HH:MM."
        ) from exc

    if not 0 <= hours <= 23:
        raise ValueError(
            f"Invalid hour in '{value}'."
        )

    if not 0 <= minutes <= 59:
        raise ValueError(
            f"Invalid minute in '{value}'."
        )

    return hours * 60 + minutes


def _from_minutes(total_minutes: int) -> str:
    """Convert absolute planning minutes to HH:MM."""

    total_minutes %= MINUTES_PER_DAY

    hours = total_minutes // 60
    minutes = total_minutes % 60

    return f"{hours:02d}:{minutes:02d}"


def _parse_date(value: Any) -> date | None:
    """Convert supported date representations into date."""

    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _normalize_interval(
    start: int,
    end: int,
) -> tuple[int, int]:
    """
    Convert local interval into a forward interval.

    Example:
        23:00 -> 04:00

    becomes:
        1380 -> 1680
    """

    if end <= start:
        end += MINUTES_PER_DAY

    return start, end


def _overlaps(
    start_a: int,
    end_a: int,
    start_b: int,
    end_b: int,
) -> bool:
    """Half-open interval overlap test."""

    return (
        start_a < end_b
        and start_b < end_a
    )


def _duration(
    start: int,
    end: int,
) -> int:
    """Return non-negative duration."""

    return max(0, end - start)


def _json_safe_date(value: Any) -> str | None:
    """
    Convert a date/datetime (or ISO string) into a plain ISO string
    for JSON serialization. Returns None for missing/invalid values.
    """

    if value is None:
        return None

    if isinstance(value, (date, datetime)):
        return value.isoformat()

    return str(value)


# ============================================================================
# GLOBAL TIMELINE
# ============================================================================

def _date_offset(
    value: date,
    base_date: date,
) -> int:
    """Return day offset from planning start date."""

    return (value - base_date).days


def _global_interval(
    scheduled_date: date,
    local_start: int,
    local_end: int,
    base_date: date,
) -> tuple[int, int]:
    """
    Convert local date/time interval into global planning minutes.
    """

    day_offset = _date_offset(
        scheduled_date,
        base_date,
    )

    global_start = (
        day_offset * MINUTES_PER_DAY
        + local_start
    )

    global_end = (
        day_offset * MINUTES_PER_DAY
        + local_end
    )

    if global_end <= global_start:
        global_end += MINUTES_PER_DAY

    return global_start, global_end


# ============================================================================
# OCCUPANCY NORMALIZATION
# ============================================================================

def _normalize_occupancy(
    occupancy: dict,
    base_date: date,
) -> tuple[int, int, int] | None:
    """
    Convert train occupancy into:

        corridor_id,
        global_start,
        global_end
    """

    corridor_id = occupancy.get("corridor_id")

    occupancy_date = _parse_date(
        occupancy.get("date")
    )

    if corridor_id is None:
        return None

    if occupancy_date is None:
        return None

    try:
        start_local = _to_minutes(
            occupancy["entry_time"]
        )

        end_local = _to_minutes(
            occupancy["exit_time"]
        )

        corridor_id = int(corridor_id)

    except (
        KeyError,
        TypeError,
        ValueError,
    ):
        return None

    start_local, end_local = _normalize_interval(
        start_local,
        end_local,
    )

    global_start, global_end = _global_interval(
        scheduled_date=occupancy_date,
        local_start=start_local,
        local_end=end_local,
        base_date=base_date,
    )

    return (
        corridor_id,
        global_start,
        global_end,
    )


def _build_occupancy_lookup(
    train_occupancies: list[dict],
    base_date: date,
) -> dict[int, list[tuple[int, int]]]:
    """
    Build:

        corridor_id -> occupancy intervals
    """

    lookup: dict[
        int,
        list[tuple[int, int]]
    ] = defaultdict(list)

    for occupancy in train_occupancies or []:

        normalized = _normalize_occupancy(
            occupancy,
            base_date,
        )

        if normalized is None:
            continue

        corridor_id, start, end = normalized

        if end <= start:
            continue

        lookup[corridor_id].append(
            (start, end)
        )

    for corridor_id in lookup:
        lookup[corridor_id].sort()

    return lookup


# ============================================================================
# FREE INTERVALS
# ============================================================================

def _compute_free_intervals(
    window_start: int,
    window_end: int,
    occupancy_ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """
    Remove train occupancy from maintenance window.
    """

    if window_end <= window_start:
        return []

    relevant = []

    for occupancy_start, occupancy_end in occupancy_ranges:

        if occupancy_end <= occupancy_start:
            continue

        overlap_start = max(
            window_start,
            occupancy_start,
        )

        overlap_end = min(
            window_end,
            occupancy_end,
        )

        if overlap_start < overlap_end:
            relevant.append(
                (
                    overlap_start,
                    overlap_end,
                )
            )

    if not relevant:
        return [
            (
                window_start,
                window_end,
            )
        ]

    relevant.sort()

    merged = []

    current_start, current_end = relevant[0]

    for start, end in relevant[1:]:

        if start <= current_end:

            current_end = max(
                current_end,
                end,
            )

        else:

            merged.append(
                (
                    current_start,
                    current_end,
                )
            )

            current_start = start
            current_end = end

    merged.append(
        (
            current_start,
            current_end,
        )
    )

    free = []

    cursor = window_start

    for blocked_start, blocked_end in merged:

        if cursor < blocked_start:
            free.append(
                (
                    cursor,
                    blocked_start,
                )
            )

        cursor = max(
            cursor,
            blocked_end,
        )

    if cursor < window_end:
        free.append(
            (
                cursor,
                window_end,
            )
        )

    return free


# ============================================================================
# CANDIDATE START GENERATION
# ============================================================================

def _find_fitting_starts(
    free_intervals: list[tuple[int, int]],
    duration_minutes: int,
) -> list[int]:
    """
    Generate a small candidate set.

    For every free interval:

        1. earliest possible start
        2. latest possible start

    This keeps the CP-SAT model small.
    """

    if duration_minutes <= 0:
        return []

    starts: set[int] = set()

    for start, end in free_intervals:

        if end - start < duration_minutes:
            continue

        earliest = start
        latest = end - duration_minutes

        starts.add(earliest)
        starts.add(latest)

    return sorted(starts)


# ============================================================================
# NORMALIZATION / OBJECTIVE HELPERS
# ============================================================================

def _safe_float(
    value: Any,
    default: float = 0.0,
) -> float:
    """Safely convert value to float."""

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(
    value: Any,
    default: int = 0,
) -> int:
    """Safely convert value to int."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _criticality_score(
    value: Any,
) -> int:
    """
    Domain:

        1 = most critical
        5 = least critical
    """

    criticality = _safe_int(value, 5)

    criticality = max(
        1,
        min(5, criticality),
    )

    return 6 - criticality


def _overdue_days(
    due_date: Any,
    today: date,
) -> int:
    """Return positive overdue days."""

    parsed_due_date = _parse_date(due_date)

    if parsed_due_date is None:
        return 0

    return max(
        0,
        (today - parsed_due_date).days,
    )


def _freight_penalty(
    window: dict,
) -> int:
    """Calculate freight impact penalty."""

    expected_goods = _safe_float(
        window.get(
            "expected_goods_trains",
            0,
        )
    )

    probability = _safe_float(
        window.get(
            "freight_probability",
            0,
        )
    )

    has_forecast_fields = (
        "expected_goods_trains" in window
        or
        "freight_probability" in window
    )

    if has_forecast_fields:

        return int(
            round(
                expected_goods * 100
                + probability * 100
            )
        )

    if not window.get(
        "is_goods_forecast_clear",
        True,
    ):
        return PENALTY_FREIGHT_IMPACT

    return 0


# ============================================================================
# CORRIDOR NORMALIZATION
# ============================================================================

def _get_task_corridor_id(
    task: dict,
    window: dict | None = None,
) -> int | None:
    """
    Get normalized corridor ID from task.

    Falls back to the window corridor.
    """

    corridor_id = task.get("corridor_id")

    if corridor_id is not None:

        try:
            return int(corridor_id)
        except (TypeError, ValueError):
            pass

    if window is not None:

        corridor_id = window.get(
            "corridor_id"
        )

        if corridor_id is not None:

            try:
                return int(corridor_id)
            except (TypeError, ValueError):
                pass

    return None


# ============================================================================
# EMPTY RESULT
# ============================================================================

def _empty_result(
    reason: str,
    tasks: list[dict],
    solve_start: float,
) -> dict:

    task_list = tasks or []

    return {
        "scheduled": [],
        "unscheduled": [
            {
                "task_id": task.get("id"),
                "task_ref": task.get("task_ref"),
                "department_code": task.get(
                    "department_code"
                ),
                "corridor_block_id": task.get(
                    "corridor_block_id"
                ),
                "defect_type": task.get(
                    "defect_type"
                ),
                "criticality": task.get(
                    "criticality"
                ),
                "due_date": _json_safe_date(
                    task.get("due_date")
                ),
                "priority_score": task.get(
                    "priority_score",
                    0,
                ),
                "reason": reason,
            }
            for task in task_list
        ],
        "optimization_summary": {
            "solver_status": reason,
            "solver_optimal": False,
            "solver_feasible": False,
            "total_scheduled": 0,
            "total_unscheduled": len(task_list),
            "total_blocks": 0,
            "total_block_minutes": 0,
            "asset_availability_score": 100.0,
            "coordinated_tasks": 0,
            "shared_blocks": 0,
            "overdue_tasks_scheduled": 0,
            "average_scheduled_priority": 0.0,
            "solve_time_ms": round(
                (time.time() - solve_start) * 1000,
                2,
            ),
            "objective": "no optimization performed",
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
    candidates: dict[tuple[int, int, int], dict],
    selected_candidates: list[tuple[int, int, int]],
    normalized_incompatible: set[frozenset[str]],
    resource_available: dict[Any, bool],
    corridor_capacities: dict[int, int],
    today: date,
) -> str:

    duration = _safe_int(
        task.get(
            "estimated_duration_minutes",
            0,
        )
    )

    if duration <= 0:
        return "invalid_duration"

    if duration > MINUTES_PER_DAY:
        return "duration_exceeds_one_day"

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

    task_candidates = [
        key
        for key in candidates
        if key[0] == task_index
    ]

    if not task_candidates:
        return "no_feasible_time_window"

    selected_task_indexes = {
        key[0]
        for key in selected_candidates
    }

    task_corridor = _get_task_corridor_id(
        task
    )

    task_defect = str(
        task.get(
            "defect_type",
            "",
        )
    ).upper().strip()

    capacity_seen = False

    for candidate_key in task_candidates:

        candidate = candidates[candidate_key]

        task_start = candidate[
            "global_start"
        ]

        task_end = candidate[
            "global_end"
        ]

        window = window_meta[
            candidate["window_index"]
        ]

        corridor_id = _get_task_corridor_id(
            task,
            window,
        )

        # ---------------------------------------------------------------
        # Check selected conflicts
        # ---------------------------------------------------------------

        for selected_key in selected_candidates:

            other_index = selected_key[0]

            if other_index == task_index:
                continue

            if other_index not in selected_task_indexes:
                continue

            other_candidate = candidates[
                selected_key
            ]

            other_task = tasks[
                other_index
            ]

            other_window = window_meta[
                other_candidate["window_index"]
            ]

            other_corridor = _get_task_corridor_id(
                other_task,
                other_window,
            )

            if corridor_id != other_corridor:
                continue

            if not _overlaps(
                task_start,
                task_end,
                other_candidate["global_start"],
                other_candidate["global_end"],
            ):
                continue

            other_resource_id = other_task.get(
                "required_resource_id"
            )

            if (
                resource_id is not None
                and resource_id == other_resource_id
            ):
                return "resource_conflict"

            other_defect = str(
                other_task.get(
                    "defect_type",
                    "",
                )
            ).upper().strip()

            if (
                frozenset(
                    (
                        task_defect,
                        other_defect,
                    )
                )
                in normalized_incompatible
            ):
                return "safety_incompatibility"

        # ---------------------------------------------------------------
        # Corridor capacity
        # ---------------------------------------------------------------

        capacity = max(
            1,
            _safe_int(
                corridor_capacities.get(
                    corridor_id,
                    1,
                ),
                1,
            ),
        )

        simultaneous = 0

        for selected_key in selected_candidates:

            selected_task = tasks[
                selected_key[0]
            ]

            selected_window = window_meta[
                candidates[selected_key][
                    "window_index"
                ]
            ]

            selected_corridor = _get_task_corridor_id(
                selected_task,
                selected_window,
            )

            if selected_corridor != corridor_id:
                continue

            selected_candidate = candidates[
                selected_key
            ]

            if _overlaps(
                task_start,
                task_end,
                selected_candidate["global_start"],
                selected_candidate["global_end"],
            ):
                simultaneous += 1

        if simultaneous >= capacity:
            capacity_seen = True

    if capacity_seen:
        return "corridor_capacity_exhausted"

    if _overdue_days(
        task.get("due_date"),
        today,
    ) > 0:
        return "optimizer_tradeoff_overdue_task"

    return "optimizer_tradeoff_lower_priority"


# ============================================================================
# MAIN OPTIMIZER
# ============================================================================

def run_cp_sat_block_planning(
    tasks: list[dict],
    windows: list[dict],
    train_occupancies: list[dict],
    incompatible_pairs: set[tuple[str, str]],
    resources: list[dict],
    corridor_capacities: dict[int, int],
    today: date,
    horizon_str: str,
) -> dict:
    """
    Run RailBlock AI CP-SAT optimization.
    """

    solve_start = time.time()

    print(
        "[OPTIMIZER] Entered "
        "run_cp_sat_block_planning()",
        flush=True,
    )

    tasks = tasks or []
    windows = windows or []
    train_occupancies = train_occupancies or []
    resources = resources or []
    incompatible_pairs = incompatible_pairs or set()
    corridor_capacities = corridor_capacities or {}

    # ========================================================================
    # STEP 0 — INPUT VALIDATION
    # ========================================================================

    if not tasks:
        return _empty_result(
            reason="NO_TASKS",
            tasks=[],
            solve_start=solve_start,
        )

    if not windows:
        return _empty_result(
            reason="NO_VALID_WINDOWS",
            tasks=tasks,
            solve_start=solve_start,
        )

    # ========================================================================
    # STEP 1 — OCCUPANCY LOOKUP
    # ========================================================================

    occupancy_lookup = _build_occupancy_lookup(
        train_occupancies=train_occupancies,
        base_date=today,
    )

    # ========================================================================
    # STEP 2 — PREPROCESS WINDOWS
    # ========================================================================

    window_meta: list[dict] = []

    for window in windows:

        try:
            window_date = _parse_date(
                window.get("date")
            )

            if window_date is None:
                continue

            start_local = _to_minutes(
                window.get("start_time")
            )

            end_local = _to_minutes(
                window.get("end_time")
            )

            start_local, end_local = (
                _normalize_interval(
                    start_local,
                    end_local,
                )
            )

            corridor_id = int(
                window.get("corridor_id")
            )

        except (
            TypeError,
            ValueError,
            KeyError,
        ):
            continue

        global_start, global_end = (
            _global_interval(
                scheduled_date=window_date,
                local_start=start_local,
                local_end=end_local,
                base_date=today,
            )
        )

        occupancy_ranges = occupancy_lookup.get(
            corridor_id,
            [],
        )

        free_intervals = _compute_free_intervals(
            window_start=global_start,
            window_end=global_end,
            occupancy_ranges=occupancy_ranges,
        )

        total_free_minutes = sum(
            _duration(start, end)
            for start, end in free_intervals
        )

        window_meta.append(
            {
                **window,
                "date": window_date,
                "corridor_id": corridor_id,
                "global_start": global_start,
                "global_end": global_end,
                "free_intervals": free_intervals,
                "total_free_minutes": total_free_minutes,
                "freight_penalty": _freight_penalty(
                    window
                ),
            }
        )

    if not window_meta:
        return _empty_result(
            reason="NO_VALID_WINDOWS",
            tasks=tasks,
            solve_start=solve_start,
        )

    # ========================================================================
    # STEP 3 — RESOURCE AVAILABILITY
    # ========================================================================

    resource_available: dict[Any, bool] = {}

    for resource in resources:

        if "id" not in resource:
            continue

        resource_id = resource["id"]

        resource_available[
            resource_id
        ] = (
            str(
                resource.get(
                    "availability_status",
                    "available",
                )
            )
            .lower()
            .strip()
            == "available"
        )

    # ========================================================================
    # STEP 4 — SAFETY RULES
    # ========================================================================

    normalized_incompatible: set[
        frozenset[str]
    ] = set()

    for pair in incompatible_pairs:

        if not pair:
            continue

        try:
            if len(pair) != 2:
                continue

            first = str(
                pair[0]
            ).upper().strip()

            second = str(
                pair[1]
            ).upper().strip()

        except (
            TypeError,
            IndexError,
        ):
            continue

        if not first or not second:
            continue

        normalized_incompatible.add(
            frozenset(
                (
                    first,
                    second,
                )
            )
        )

    # ========================================================================
    # STEP 5 — BUILD CANDIDATES
    # ========================================================================

    candidates: dict[
        tuple[int, int, int],
        dict,
    ] = {}

    infeasibility_reasons: dict[
        int,
        set[str],
    ] = defaultdict(set)

    for task_index, task in enumerate(tasks):

        duration = _safe_int(
            task.get(
                "estimated_duration_minutes",
                0,
            )
        )

        # ---------------------------------------------------------------
        # Validate duration
        # ---------------------------------------------------------------

        if duration <= 0:

            infeasibility_reasons[
                task_index
            ].add(
                "invalid_duration"
            )

            continue

        if duration > MINUTES_PER_DAY:

            infeasibility_reasons[
                task_index
            ].add(
                "duration_exceeds_one_day"
            )

            continue

        # ---------------------------------------------------------------
        # Validate resource
        # ---------------------------------------------------------------

        resource_id = task.get(
            "required_resource_id"
        )

        if resource_id is not None:

            # If resources are provided, the resource must exist
            # and be available.
            if (
                resources
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

        # ---------------------------------------------------------------
        # Match task to corridor/window
        # ---------------------------------------------------------------

        task_corridor_id = task.get(
            "corridor_id"
        )

        matching_windows: list[
            tuple[int, dict]
        ] = []

        if task_corridor_id is not None:

            try:
                task_corridor_id = int(
                    task_corridor_id
                )
            except (
                TypeError,
                ValueError,
            ):
                task_corridor_id = None

        if task_corridor_id is not None:

            matching_windows = [
                (
                    index,
                    window,
                )
                for index, window
                in enumerate(window_meta)
                if window.get(
                    "corridor_id"
                ) == task_corridor_id
            ]

        else:

            task_block_id = str(
                task.get(
                    "corridor_block_id",
                    "",
                )
            ).upper().strip()

            if task_block_id:

                matching_windows = [
                    (
                        index,
                        window,
                    )
                    for index, window
                    in enumerate(window_meta)
                    if str(
                        window.get(
                            "corridor_block_id",
                            "",
                        )
                    ).upper().strip()
                    == task_block_id
                ]

        if not matching_windows:

            infeasibility_reasons[
                task_index
            ].add(
                "no_matching_corridor"
            )

            continue

        # ---------------------------------------------------------------
        # Generate candidate starts
        # ---------------------------------------------------------------

        for window_index, window in matching_windows:

            free_intervals = window[
                "free_intervals"
            ]

            starts = _find_fitting_starts(
                free_intervals=free_intervals,
                duration_minutes=duration,
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

                if end > window["global_end"]:
                    continue

                # Exact free interval check
                inside_free_interval = any(
                    start >= free_start
                    and end <= free_end
                    for free_start, free_end
                    in free_intervals
                )

                if not inside_free_interval:
                    continue

                key = (
                    task_index,
                    window_index,
                    start,
                )

                candidates[key] = {
                    "task_index": task_index,
                    "window_index": window_index,
                    "start": start,
                    "end": end,
                    "global_start": start,
                    "global_end": end,
                }

    # ========================================================================
    # NO CANDIDATES
    # ========================================================================

    if not candidates:

        result = _empty_result(
            reason="NO_FEASIBLE_CANDIDATES",
            tasks=tasks,
            solve_start=solve_start,
        )

        result["unscheduled"] = []

        for task_index, task in enumerate(tasks):

            reasons = infeasibility_reasons[
                task_index
            ]

            if "invalid_duration" in reasons:
                reason = "invalid_duration"

            elif "duration_exceeds_one_day" in reasons:
                reason = "duration_exceeds_one_day"

            elif "resource_unavailable" in reasons:
                reason = "resource_unavailable"

            elif "no_matching_corridor" in reasons:
                reason = "no_matching_corridor"

            elif "insufficient_window_duration" in reasons:
                reason = "insufficient_window_duration"

            else:
                reason = "no_feasible_time_window"

            result["unscheduled"].append(
                {
                    "task_id": task.get("id"),
                    "task_ref": task.get("task_ref"),
                    "department_code": task.get(
                        "department_code"
                    ),
                    "corridor_block_id": task.get(
                        "corridor_block_id"
                    ),
                    "defect_type": task.get(
                        "defect_type"
                    ),
                    "criticality": task.get(
                        "criticality"
                    ),
                    "due_date": _json_safe_date(
                        task.get("due_date")
                    ),
                    "priority_score": task.get(
                        "priority_score",
                        0,
                    ),
                    "reason": reason,
                }
            )

        result[
            "optimization_summary"
        ][
            "total_unscheduled"
        ] = len(
            result["unscheduled"]
        )

        return result

    # ========================================================================
    # STEP 6 — CREATE CP-SAT MODEL
    # ========================================================================

    model = cp_model.CpModel()

    assign: dict[
        tuple[int, int, int],
        cp_model.IntVar,
    ] = {}

    intervals: dict[
        tuple[int, int, int],
        cp_model.IntervalVar,
    ] = {}

    for key, candidate in candidates.items():

        task_index = candidate[
            "task_index"
        ]

        start = candidate[
            "global_start"
        ]

        duration = _safe_int(
            tasks[
                task_index
            ].get(
                "estimated_duration_minutes",
                0,
            )
        )

        presence = model.new_bool_var(
            (
                f"assign_t{task_index}"
                f"_w{candidate['window_index']}"
                f"_s{start}"
            )
        )

        assign[key] = presence

        intervals[key] = (
            model.new_optional_fixed_size_interval_var(
                start,
                duration,
                presence,
                (
                    f"interval_t{task_index}"
                    f"_w{candidate['window_index']}"
                    f"_s{start}"
                ),
            )
        )

    # ========================================================================
    # CONSTRAINT 1 — EACH TASK AT MOST ONCE
    # ========================================================================

    candidates_by_task: dict[
        int,
        list[cp_model.IntVar],
    ] = defaultdict(list)

    for key, variable in assign.items():

        candidates_by_task[
            key[0]
        ].append(variable)

    for variables in candidates_by_task.values():

        model.add(
            sum(variables) <= 1
        )

    # ========================================================================
    # CONSTRAINT 2 — RESOURCE OVERLAP
    # ========================================================================

    resource_groups: dict[
        Any,
        list[cp_model.IntervalVar],
    ] = defaultdict(list)

    for key in candidates:

        task_index = key[0]

        resource_id = tasks[
            task_index
        ].get(
            "required_resource_id"
        )

        if resource_id is None:
            continue

        resource_groups[
            resource_id
        ].append(
            intervals[key]
        )

    for resource_id, group in resource_groups.items():

        if not group:
            continue

        model.add_cumulative(
            group,
            [1] * len(group),
            1,
        )

    # ========================================================================
    # CONSTRAINT 3 — SAFETY INCOMPATIBILITY
    # ========================================================================

    candidate_items = list(
        assign.items()
    )

    for index_a in range(
        len(candidate_items)
    ):

        key_a, var_a = candidate_items[
            index_a
        ]

        candidate_a = candidates[key_a]

        task_a_index = candidate_a[
            "task_index"
        ]

        task_a = tasks[
            task_a_index
        ]

        window_a = window_meta[
            candidate_a["window_index"]
        ]

        corridor_a = _get_task_corridor_id(
            task_a,
            window_a,
        )

        defect_a = str(
            task_a.get(
                "defect_type",
                "",
            )
        ).upper().strip()

        for index_b in range(
            index_a + 1,
            len(candidate_items),
        ):

            key_b, var_b = candidate_items[
                index_b
            ]

            candidate_b = candidates[key_b]

            task_b_index = candidate_b[
                "task_index"
            ]

            # Same task is already limited by <= 1
            if task_a_index == task_b_index:
                continue

            task_b = tasks[
                task_b_index
            ]

            window_b = window_meta[
                candidate_b["window_index"]
            ]

            corridor_b = _get_task_corridor_id(
                task_b,
                window_b,
            )

            if corridor_a != corridor_b:
                continue

            if not _overlaps(
                candidate_a["global_start"],
                candidate_a["global_end"],
                candidate_b["global_start"],
                candidate_b["global_end"],
            ):
                continue

            defect_b = str(
                task_b.get(
                    "defect_type",
                    "",
                )
            ).upper().strip()

            defect_pair = frozenset(
                (
                    defect_a,
                    defect_b,
                )
            )

            if defect_pair in normalized_incompatible:

                model.add(
                    var_a + var_b <= 1
                )

    # ========================================================================
    # CONSTRAINT 4 — CORRIDOR CAPACITY
    # ========================================================================

    corridor_groups: dict[
        int,
        list[cp_model.IntervalVar],
    ] = defaultdict(list)

    for key in candidates:

        task_index = key[0]

        task = tasks[
            task_index
        ]

        window = window_meta[
            key[1]
        ]

        corridor_id = _get_task_corridor_id(
            task,
            window,
        )

        if corridor_id is None:
            continue

        corridor_groups[
            corridor_id
        ].append(
            intervals[key]
        )

    for corridor_id, group in corridor_groups.items():

        capacity = max(
            1,
            _safe_int(
                corridor_capacities.get(
                    corridor_id,
                    1,
                ),
                1,
            ),
        )

        model.add_cumulative(
            group,
            [1] * len(group),
            capacity,
        )

    # ========================================================================
    # STEP 7 — OBJECTIVE
    # ========================================================================

    objective_terms = []

    for key, variable in assign.items():

        task_index = key[0]
        window_index = key[1]

        candidate = candidates[key]
        task = tasks[task_index]
        window = window_meta[window_index]

        # ---------------------------------------------------------------
        # Priority
        # ---------------------------------------------------------------

        priority_score = _safe_float(
            task.get(
                "priority_score",
                0,
            )
        )

        priority_component = int(
            round(
                priority_score
                * WEIGHT_PRIORITY
            )
        )

        # ---------------------------------------------------------------
        # Criticality
        # ---------------------------------------------------------------

        criticality_component = (
            _criticality_score(
                task.get(
                    "criticality",
                    5,
                )
            )
            * WEIGHT_CRITICALITY
        )

        # ---------------------------------------------------------------
        # Asset impact
        # ---------------------------------------------------------------

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

        # ---------------------------------------------------------------
        # Overdue
        # ---------------------------------------------------------------

        overdue_component = (
            min(
                _overdue_days(
                    task.get(
                        "due_date"
                    ),
                    today,
                ),
                30,
            )
            * WEIGHT_OVERDUE
        )

        # ---------------------------------------------------------------
        # Freight impact
        # ---------------------------------------------------------------

        freight_component = _freight_penalty(
            window
        )

        # ---------------------------------------------------------------
        # Duration penalty
        # ---------------------------------------------------------------

        duration = _safe_int(
            task.get(
                "estimated_duration_minutes",
                0,
            )
        )

        duration_penalty = (
            duration
            * PENALTY_BLOCK_MINUTE
        )

        # ---------------------------------------------------------------
        # Late-day penalty
        # ---------------------------------------------------------------

        local_minute = (
            candidate["global_start"]
            % MINUTES_PER_DAY
        )

        late_day_hours = max(
            0,
            (
                local_minute
                - 18 * 60
            ) / 60,
        )

        late_day_penalty = int(
            round(
                late_day_hours
                * PENALTY_LATE_DAY
            )
        )

        candidate_score = (
            priority_component
            + criticality_component
            + asset_component
            + overdue_component
            - freight_component
            - duration_penalty
            - late_day_penalty
        )

        objective_terms.append(
            candidate_score * variable
        )

    # Small penalty per selected candidate.
    for variable in assign.values():

        objective_terms.append(
            -1 * variable
        )

    if objective_terms:

        model.maximize(
            sum(objective_terms)
        )

    # ========================================================================
    # STEP 8 — SOLVE
    # ========================================================================

    solver = cp_model.CpSolver()

    # Safety limit for hackathon/demo
    solver.parameters.max_time_in_seconds = 10.0

    # Use multiple CPU workers
    solver.parameters.num_search_workers = 8

    print("[OPTIMIZER] About to call solver.Solve()")
    

    status = solver.Solve(model)

    print(f"[OPTIMIZER] solver.Solve() returned")
    print(f"[OPTIMIZER] status = {solver.StatusName(status)}")
    print(f"[OPTIMIZER] objective = {solver.ObjectiveValue()}")
    print(f"[OPTIMIZER] best_bound = {solver.BestObjectiveBound()}")

    solver.parameters.max_time_in_seconds = (
        SOLVER_TIME_LIMIT_SECONDS
    )

    solver.parameters.num_search_workers = (
        SOLVER_WORKERS
    )

    solver.parameters.random_seed = 42

    solver.parameters.log_search_progress = False

    solver.parameters.cp_model_presolve = True

    print(
        f"[OPTIMIZER] Starting CP-SAT solve. "
        f"candidates={len(candidates)}, "
        f"tasks={len(tasks)}, "
        f"windows={len(window_meta)}",
        flush=True,
    )

    # ------------------------------------------------------------------------
    # IMPORTANT:
    #
    # The return belongs INSIDE except.
    #
    # If solve() succeeds, execution must continue to STEP 9.
    # ------------------------------------------------------------------------

    try:

        print("[OPTIMIZER] About to call solver.Solve()")

        status = solver.Solve(model)

        print(f"[OPTIMIZER] solver.Solve() returned. status={status}")
        print(f"[OPTIMIZER] Objective value={solver.ObjectiveValue()}")
        print(f"[OPTIMIZER] Best bound={solver.BestObjectiveBound()}")

        print(
            f"[OPTIMIZER] CP-SAT returned. "
            f"status={solver.status_name(status)}",
            flush=True,
        )

    except Exception as exc:

        print(
            f"[OPTIMIZER] CP-SAT ERROR: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

        return _empty_result(
            reason=(
                "SOLVER_ERROR: "
                f"{type(exc).__name__}"
            ),
            tasks=tasks,
            solve_start=solve_start,
        )

    solve_ms = (
        time.time()
        - solve_start
    ) * 1000

    status_name = solver.status_name(
        status
    )

    print(
        f"[OPTIMIZER] Solve time: {round(solve_ms, 2)} ms",
        flush=True,
    )

    print(
        f"[OPTIMIZER] candidates={len(candidates)}, "
        f"tasks={len(tasks)}, "
        f"solver_status={status_name}",
        flush=True,
    )

    # ========================================================================
    # STEP 8A — INVALID MODEL
    # ========================================================================

    if status == cp_model.MODEL_INVALID:

        raise ValueError(
            "CP-SAT model is invalid. "
            "This indicates a bug in constraint construction."
        )

    # ========================================================================
    # STEP 8B — NO FEASIBLE SOLUTION
    # ========================================================================

    if status not in (
        cp_model.OPTIMAL,
        cp_model.FEASIBLE,
    ):

        if status == cp_model.INFEASIBLE:

            reason = "solver_infeasible"

        elif status == cp_model.UNKNOWN:

            reason = "solver_unknown"

        else:

            reason = (
                "solver_"
                + status_name.lower()
            )

        print(
            f"[OPTIMIZER] Done. scheduled=0, "
            f"unscheduled={len(tasks)}, "
            f"candidates={len(candidates)}, "
            f"reason={reason}",
            flush=True,
        )

        return {
            "scheduled": [],
            "unscheduled": [
                {
                    "task_id": task.get("id"),
                    "task_ref": task.get("task_ref"),
                    "department_code": task.get(
                        "department_code"
                    ),
                    "corridor_block_id": task.get(
                        "corridor_block_id"
                    ),
                    "defect_type": task.get(
                        "defect_type"
                    ),
                    "criticality": task.get(
                        "criticality"
                    ),
                    "due_date": _json_safe_date(
                        task.get("due_date")
                    ),
                    "priority_score": task.get(
                        "priority_score",
                        0,
                    ),
                    "reason": reason,
                }
                for task in tasks
            ],
            "optimization_summary": {
                "solver_status": status_name,
                "solver_optimal": False,
                "solver_feasible": False,
                "total_scheduled": 0,
                "total_unscheduled": len(tasks),
                "total_blocks": 0,
                "total_block_minutes": 0,
                "asset_availability_score": 100.0,
                "coordinated_tasks": 0,
                "shared_blocks": 0,
                "overdue_tasks_scheduled": 0,
                "average_scheduled_priority": 0.0,
                "solve_time_ms": round(
                    solve_ms,
                    2,
                ),
                "objective": (
                    "no feasible solution found"
                ),
            },
        }

    # ========================================================================
    # STEP 9 — EXTRACT SOLUTION
    # ========================================================================

    selected_candidates: list[
        tuple[int, int, int]
    ] = []

    task_scheduled: dict[
        int,
        bool,
    ] = {
        index: False
        for index in range(
            len(tasks)
        )
    }

    for key, variable in assign.items():

        if solver.boolean_value(variable):

            selected_candidates.append(
                key
            )

            task_scheduled[
                key[0]
            ] = True

    # ========================================================================
    # STEP 10 — BUILD PHYSICAL BLOCK GROUPS
    # ========================================================================

    uf_parent: dict[
        tuple[int, int, int],
        tuple[int, int, int],
    ] = {
        key: key
        for key in selected_candidates
    }

    def uf_find(
        value: tuple[int, int, int],
    ) -> tuple[int, int, int]:

        current = value

        while (
            uf_parent[current]
            != current
        ):

            uf_parent[current] = (
                uf_parent[
                    uf_parent[current]
                ]
            )

            current = uf_parent[
                current
            ]

        return current

    def uf_union(
        first: tuple[int, int, int],
        second: tuple[int, int, int],
    ) -> None:

        root_first = uf_find(first)
        root_second = uf_find(second)

        if root_first != root_second:

            uf_parent[
                root_first
            ] = root_second

    # ------------------------------------------------------------------------
    # Connect overlapping tasks on same corridor
    # ------------------------------------------------------------------------

    for index_a in range(
        len(selected_candidates)
    ):

        candidate_a = selected_candidates[
            index_a
        ]

        data_a = candidates[
            candidate_a
        ]

        task_a = tasks[
            data_a["task_index"]
        ]

        window_a = window_meta[
            data_a["window_index"]
        ]

        corridor_a = _get_task_corridor_id(
            task_a,
            window_a,
        )

        for index_b in range(
            index_a + 1,
            len(selected_candidates),
        ):

            candidate_b = selected_candidates[
                index_b
            ]

            data_b = candidates[
                candidate_b
            ]

            task_b = tasks[
                data_b["task_index"]
            ]

            window_b = window_meta[
                data_b["window_index"]
            ]

            corridor_b = _get_task_corridor_id(
                task_b,
                window_b,
            )

            if corridor_a != corridor_b:
                continue

            if _overlaps(
                data_a["global_start"],
                data_a["global_end"],
                data_b["global_start"],
                data_b["global_end"],
            ):

                uf_union(
                    candidate_a,
                    candidate_b,
                )

    block_groups: dict[
        tuple[int, int, int],
        list[tuple[int, int, int]],
    ] = defaultdict(list)

    for candidate in selected_candidates:

        block_groups[
            uf_find(candidate)
        ].append(candidate)

    # ========================================================================
    # STEP 11 — CREATE BLOCK GROUP IDS
    # ========================================================================

    block_group_lookup: dict[
        tuple[int, int, int],
        str,
    ] = {}

    total_block_minutes = 0
    shared_blocks = 0
    coordinated_tasks = 0

    for group_number, group in enumerate(
        block_groups.values(),
        start=1,
    ):

        if not group:
            continue

        group_start = min(
            candidates[candidate][
                "global_start"
            ]
            for candidate in group
        )

        group_end = max(
            candidates[candidate][
                "global_end"
            ]
            for candidate in group
        )

        total_block_minutes += (
            group_end - group_start
        )

        departments = {
            tasks[candidate[0]].get(
                "department_id"
            )
            for candidate in group
        }

        departments.discard(None)

        if len(departments) > 1:

            shared_blocks += 1

            coordinated_tasks += len(group)

        first_candidate = group[0]

        first_window = window_meta[
            candidates[first_candidate][
                "window_index"
            ]
        ]

        block_group_id = (
            f"BG"
            f"{str(horizon_str)[0].upper()}"
            f"-{first_window.get('id')}"
            f"-{group_start}"
            f"-{group_number}"
        )

        for candidate in group:

            block_group_lookup[
                candidate
            ] = block_group_id

    # ========================================================================
    # STEP 12 — SCHEDULED OUTPUT
    # ========================================================================

    scheduled: list[dict] = []

    for candidate in selected_candidates:

        task_index = candidate[0]
        window_index = candidate[1]
        start = candidate[2]

        task = tasks[task_index]
        window = window_meta[window_index]

        duration = _safe_int(
            task.get(
                "estimated_duration_minutes",
                0,
            )
        )

        end = start + duration

        scheduled_date = window["date"]

        # Use task corridor first, then window corridor.
        corridor_id = _get_task_corridor_id(
            task,
            window,
        )

        scheduled.append(
            {
                "task_id": task.get("id"),
                "task_ref": task.get("task_ref"),
                "department_id": task.get(
                    "department_id"
                ),
                "department_code": task.get(
                    "department_code"
                ),
                "corridor_id": corridor_id,
                "corridor_block_id": task.get(
                    "corridor_block_id"
                ),
                "defect_type": task.get(
                    "defect_type"
                ),
                "window_id": window.get(
                    "id"
                ),
                "block_group_id": block_group_lookup[
                    candidate
                ],
                "scheduled_date": _json_safe_date(
                    scheduled_date
                ),
                "entry_time": _from_minutes(
                    start
                ),
                "exit_time": _from_minutes(
                    end
                ),
                "priority_score": task.get(
                    "priority_score",
                    0,
                ),
                "criticality": task.get(
                    "criticality"
                ),
                "asset_impact_score": task.get(
                    "asset_impact_score",
                    0,
                ),
                "resource_id": task.get(
                    "required_resource_id"
                ),
                "status": (
                    "OPTIMAL"
                    if status == cp_model.OPTIMAL
                    else "FEASIBLE"
                ),
            }
        )

    # ========================================================================
    # STEP 13 — UNSCHEDULED TASKS
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
            normalized_incompatible=(
                normalized_incompatible
            ),
            resource_available=resource_available,
            corridor_capacities=corridor_capacities,
            today=today,
        )

        # Prefer direct preprocessing reasons.
        reasons = infeasibility_reasons[
            task_index
        ]

        if "invalid_duration" in reasons:

            reason = "invalid_duration"

        elif "duration_exceeds_one_day" in reasons:

            reason = "duration_exceeds_one_day"

        elif "resource_unavailable" in reasons:

            reason = "resource_unavailable"

        elif "no_matching_corridor" in reasons:

            reason = "no_matching_corridor"

        elif "insufficient_window_duration" in reasons:

            reason = "insufficient_window_duration"

        unscheduled.append(
            {
                "task_id": task.get("id"),
                "task_ref": task.get("task_ref"),
                "department_code": task.get(
                    "department_code"
                ),
                "corridor_block_id": task.get(
                    "corridor_block_id"
                ),
                "defect_type": task.get(
                    "defect_type"
                ),
                "criticality": task.get(
                    "criticality"
                ),
                "due_date": _json_safe_date(
                    task.get("due_date")
                ),
                "priority_score": task.get(
                    "priority_score",
                    0,
                ),
                "reason": reason,
            }
        )

    # ========================================================================
    # STEP 14 — KPI METRICS
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
                    100.0 * (
                        1.0 - utilization
                    ),
                ),
            ),
            2,
        )

    else:

        asset_availability_score = 100.0

    total_blocks = len(block_groups)

    average_scheduled_priority = (
        round(
            sum(
                _safe_float(
                    entry.get(
                        "priority_score",
                        0,
                    )
                )
                for entry in scheduled
            )
            / len(scheduled),
            2,
        )
        if scheduled
        else 0.0
    )

    overdue_tasks_scheduled = sum(
        1
        for candidate in selected_candidates
        if _overdue_days(
            tasks[candidate[0]].get(
                "due_date"
            ),
            today,
        ) > 0
    )

    # ========================================================================
    # FINAL RESULT
    # ========================================================================

    print(
        f"[OPTIMIZER] Done. scheduled={len(scheduled)}, "
        f"unscheduled={len(unscheduled)}, "
        f"candidates={len(candidates)}",
        flush=True,
    )

    return {
        "scheduled": scheduled,

        "unscheduled": unscheduled,

        "optimization_summary": {
            "solver_status": status_name,

            "solver_optimal": (
                status == cp_model.OPTIMAL
            ),

            "solver_feasible": (
                status in (
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
                "priority + criticality "
                "+ asset impact "
                "+ overdue completion "
                "- freight impact "
                "- maintenance duration "
                "- late-day preference"
            ),
        },
    }