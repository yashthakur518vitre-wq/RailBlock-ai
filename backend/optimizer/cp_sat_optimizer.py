"""
optimizer/cp_sat_optimizer.py

RailBlock AI — Integrated Railway Maintenance Block Optimizer

Hardened CP-SAT implementation.

Key guarantees:
- tasks are scheduled at most once;
- maintenance stays inside availability windows;
- train occupancy, including overnight occupancy, is excluded;
- unavailable resources are rejected;
- a resource cannot run overlapping tasks on the same date;
- safety-incompatible overlapping tasks are rejected;
- corridor capacity is enforced with CP-SAT cumulative constraints;
- cross-department overlapping work can form one coordinated possession;
- block count is explicitly penalized in the objective;
- selected tasks are grouped into true connected overlap components;
- unscheduled tasks receive a best-effort human-readable diagnosis.

This is a decision-support optimizer, not a railway safety authority.
Final operational authorization remains subject to human approval and
railway rules.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from ortools.sat.python import cp_model


# ============================================================================
# CONSTANTS
# ============================================================================

SOLVER_TIME_LIMIT_SECONDS = 60.0
SOLVER_WORKERS = 8

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

MINUTES_PER_DAY = 24 * 60


# ============================================================================
# TIME HELPERS
# ============================================================================

def _to_minutes(time_str: str) -> int:
    """Convert HH:MM to minutes from midnight."""
    if not time_str:
        raise ValueError("Time cannot be empty.")

    parts = str(time_str).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time '{time_str}'. Expected HH:MM.")

    hours, minutes = map(int, parts)
    if not 0 <= hours <= 23:
        raise ValueError(f"Invalid hour in '{time_str}'.")
    if not 0 <= minutes <= 59:
        raise ValueError(f"Invalid minute in '{time_str}'.")

    return hours * 60 + minutes


def _from_minutes(total_minutes: int) -> str:
    """Convert minutes from midnight to HH:MM, wrapping at midnight."""
    total_minutes %= MINUTES_PER_DAY
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    """Return True when two half-open intervals overlap."""
    return start_a < end_b and start_b < end_a


def _duration(start: int, end: int) -> int:
    return max(0, end - start)


# ============================================================================
# FREE INTERVAL CALCULATION
# ============================================================================

def _compute_free_intervals(
    window_start: int,
    window_end: int,
    occupancy_ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Subtract train occupancy from a maintenance window."""
    if window_end <= window_start:
        return []

    clipped: list[tuple[int, int]] = []

    for start, end in occupancy_ranges:
        if end <= start:
            continue

        overlap_start = max(start, window_start)
        overlap_end = min(end, window_end)
        if overlap_start < overlap_end:
            clipped.append((overlap_start, overlap_end))

    if not clipped:
        return [(window_start, window_end)]

    clipped.sort()
    merged: list[tuple[int, int]] = []
    current_start, current_end = clipped[0]

    for start, end in clipped[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            merged.append((current_start, current_end))
            current_start, current_end = start, end

    merged.append((current_start, current_end))

    free: list[tuple[int, int]] = []
    cursor = window_start

    for start, end in merged:
        if cursor < start:
            free.append((cursor, start))
        cursor = max(cursor, end)

    if cursor < window_end:
        free.append((cursor, window_end))

    return free


def _find_fitting_starts(
    free_intervals: list[tuple[int, int]],
    duration_minutes: int,
) -> list[int]:
    """Expose beginning- and end-aligned feasible starts."""
    if duration_minutes <= 0:
        return []

    starts: list[int] = []
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
    """Existing scale: 1 is most critical and 5 is least critical."""
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


def _overdue_days(due_date: Any, today: date) -> int:
    if not due_date:
        return 0

    try:
        return max(0, (today - due_date).days)
    except Exception:
        return 0


def _freight_penalty(window: dict) -> int:
    """
    Convert freight forecast information into a penalty.

    The scheduling service currently treats `is_goods_forecast_clear=False`
    as a hard filter, so production requests normally contain only clear
    windows. The extra fields remain supported for direct optimizer use.
    """
    expected_goods = _safe_float(window.get("expected_goods_trains"), 0.0)
    probability = _safe_float(window.get("freight_probability"), 0.0)

    if "expected_goods_trains" not in window and "freight_probability" not in window:
        return 0 if window.get("is_goods_forecast_clear", True) else PENALTY_FREIGHT_IMPACT

    return int(round(expected_goods * 20.0 + probability * 100.0))


# ============================================================================
# OCCUPANCY INDEX
# ============================================================================

def _build_occupancy_lookup(
    train_occupancies: list[dict],
) -> dict[tuple[int, date], list[tuple[int, int]]]:
    """
    Build a date-aware occupancy index.

    Overnight trains are split at midnight so a train such as
    23:30 -> 01:30 blocks both:
        current date: 23:30 -> 24:00
        next date:    00:00 -> 01:30
    """
    lookup: dict[tuple[int, date], list[tuple[int, int]]] = {}

    for occupancy in train_occupancies:
        try:
            corridor_id = occupancy["corridor_id"]
            occupancy_date = occupancy["date"]
            start = _to_minutes(occupancy["entry_time"])
            end = _to_minutes(occupancy["exit_time"])
        except (KeyError, TypeError, ValueError):
            continue

        key = (corridor_id, occupancy_date)

        if end > start:
            lookup.setdefault(key, []).append((start, end))
            continue

        # Treat end <= start as an overnight occupancy. This also gives a
        # conservative representation for a 24-hour interval when equal.
        lookup.setdefault(key, []).append((start, MINUTES_PER_DAY))
        next_key = (corridor_id, occupancy_date + timedelta(days=1))
        lookup.setdefault(next_key, []).append((0, end))

    return lookup


def _window_occupancy_ranges(
    occupancy_lookup: dict[tuple[int, date], list[tuple[int, int]]],
    corridor_id: int,
    window_date: date,
    window_start: int,
    window_end: int,
) -> list[tuple[int, int]]:
    """Return occupancy ranges aligned to the window's local timeline."""
    ranges = list(occupancy_lookup.get((corridor_id, window_date), []))

    # Overnight maintenance window: next-day occupancy is shifted by 24h.
    if window_end > MINUTES_PER_DAY:
        next_day_ranges = occupancy_lookup.get(
            (corridor_id, window_date + timedelta(days=1)),
            [],
        )
        ranges.extend(
            (start + MINUTES_PER_DAY, end + MINUTES_PER_DAY)
            for start, end in next_day_ranges
        )

    return ranges


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
    """Generate an optimized railway maintenance block plan."""

    solve_start = time.time()

    if not tasks:
        return _empty_result("no_tasks", horizon_str, solve_start)

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
                "solve_time_ms": round((time.time() - solve_start) * 1000, 2),
                "objective": "no availability windows",
            },
        }

    # ========================================================================
    # STEP 1 — TRAIN OCCUPANCY INDEX
    # ========================================================================

    occupancy_lookup = _build_occupancy_lookup(train_occupancies)

    # ========================================================================
    # STEP 2 — PREPROCESS WINDOWS
    # ========================================================================

    window_meta: list[dict] = []

    for window in windows:
        try:
            start = _to_minutes(window["start_time"])
            end = _to_minutes(window["end_time"])
        except (ValueError, TypeError, KeyError):
            continue

        if end <= start:
            end += MINUTES_PER_DAY

        occupancy_ranges = _window_occupancy_ranges(
            occupancy_lookup,
            window["corridor_id"],
            window["date"],
            start,
            end,
        )

        free_intervals = _compute_free_intervals(
            start,
            end,
            occupancy_ranges,
        )

        window_meta.append(
            {
                **window,
                "w_start": start,
                "w_end": end,
                "free_intervals": free_intervals,
                "total_free_minutes": sum(
                    _duration(s, e) for s, e in free_intervals
                ),
                "freight_penalty": _freight_penalty(window),
            }
        )

    # ========================================================================
    # STEP 3 — RESOURCE AVAILABILITY
    # ========================================================================

    resource_available: dict[Any, bool] = {
        resource["id"]: resource.get("availability_status", "available") == "available"
        for resource in resources
        if "id" in resource
    }

    # ========================================================================
    # STEP 4 — NORMALIZE SAFETY RULES
    # ========================================================================

    normalized_incompatible: set[frozenset[str]] = set()
    for pair in incompatible_pairs:
        if len(pair) != 2:
            continue
        a, b = (str(x).upper() for x in pair)
        normalized_incompatible.add(frozenset((a, b)))

    # ========================================================================
    # STEP 5 — BUILD CANDIDATE START TIMES
    # ========================================================================

    candidates: dict[tuple[int, int, int], dict] = {}
    infeasibility_reasons: dict[int, set[str]] = {
        i: set() for i in range(len(tasks))
    }

    for task_index, task in enumerate(tasks):
        try:
            duration = int(task.get("estimated_duration_minutes", 0))
        except (TypeError, ValueError):
            duration = 0

        if duration <= 0:
            infeasibility_reasons[task_index].add("invalid_duration")
            continue

        resource_id = task.get("required_resource_id")
        if (
            resource_id is not None
            and not resource_available.get(resource_id, False)
        ):
            infeasibility_reasons[task_index].add("resource_unavailable")
            continue

        for window_index, window in enumerate(window_meta):
            if task.get("corridor_id") != window.get("corridor_id"):
                continue

            starts = _find_fitting_starts(window["free_intervals"], duration)
            if not starts:
                infeasibility_reasons[task_index].add("insufficient_window_duration")
                continue

            for start in starts:
                candidates[(task_index, window_index, start)] = {
                    "task_index": task_index,
                    "window_index": window_index,
                    "start": start,
                    "end": start + duration,
                }

    # ========================================================================
    # STEP 6 — CP-SAT MODEL
    # ========================================================================

    model = cp_model.CpModel()

    assign: dict[tuple[int, int, int], cp_model.IntVar] = {}
    intervals: dict[tuple[int, int, int], cp_model.IntervalVar] = {}

    for key in candidates:
        task_index, window_index, start = key
        duration = int(tasks[task_index]["estimated_duration_minutes"])

        presence = model.new_bool_var(
            f"assign_t{task_index}_w{window_index}_s{start}"
        )
        assign[key] = presence
        intervals[key] = model.new_optional_fixed_size_interval_var(
            start,
            duration,
            presence,
            f"interval_t{task_index}_w{window_index}_s{start}",
        )

    candidate_items = list(assign.items())

    # ========================================================================
    # CONSTRAINT 1 — TASK AT MOST ONCE
    # ========================================================================

    for task_index in range(len(tasks)):
        variables = [
            variable
            for (candidate_task, _, _), variable in assign.items()
            if candidate_task == task_index
        ]
        if variables:
            model.add(sum(variables) <= 1)

    # ========================================================================
    # CONSTRAINT 2 — RESOURCE OVERLAP
    # ========================================================================

    # A resource is a unary renewable resource: capacity = 1.
    # Using cumulative here is exact and avoids O(n^2) pairwise overlap rules.
    resource_groups: dict[tuple[Any, date], list[tuple[cp_model.IntervalVar, int]]] = defaultdict(list)

    for key in candidates:
        task_index, window_index, _ = key
        resource_id = tasks[task_index].get("required_resource_id")
        if resource_id is None:
            continue

        resource_groups[
            (resource_id, window_meta[window_index]["date"])
        ].append((intervals[key], 1))

    for group_intervals in resource_groups.values():
        model.add_cumulative(
            [interval for interval, _ in group_intervals],
            [demand for _, demand in group_intervals],
            1,
        )

    # ========================================================================
    # CONSTRAINT 3 — SAFETY INCOMPATIBILITY
    # ========================================================================

    for index_a in range(len(candidate_items)):
        key_a, var_a = candidate_items[index_a]
        task_a_idx, window_a_idx, start_a = key_a
        task_a = tasks[task_a_idx]
        end_a = start_a + int(task_a["estimated_duration_minutes"])

        for index_b in range(index_a + 1, len(candidate_items)):
            key_b, var_b = candidate_items[index_b]
            task_b_idx, window_b_idx, start_b = key_b
            if task_a_idx == task_b_idx:
                continue

            task_b = tasks[task_b_idx]
            if window_meta[window_a_idx]["date"] != window_meta[window_b_idx]["date"]:
                continue
            if window_meta[window_a_idx]["corridor_id"] != window_meta[window_b_idx]["corridor_id"]:
                continue

            defect_pair = frozenset(
                (
                    str(task_a.get("defect_type", "")).upper(),
                    str(task_b.get("defect_type", "")).upper(),
                )
            )
            if defect_pair not in normalized_incompatible:
                continue

            end_b = start_b + int(task_b["estimated_duration_minutes"])
            if _overlaps(start_a, end_a, start_b, end_b):
                model.add(var_a + var_b <= 1)

    # ========================================================================
    # CONSTRAINT 4 — CORRIDOR CAPACITY
    # ========================================================================

    # IMPORTANT:
    # Capacity is enforced with AddCumulative, not pairwise constraints.
    # Therefore capacity=2 correctly prevents 3 simultaneous tasks.
    #
    # The existing RailBlock business rule treats overlapping work from
    # different departments as one coordinated possession. Accordingly,
    # capacity is enforced independently for each department.
    corridor_groups: dict[tuple[int, date, Any], list[cp_model.IntervalVar]] = defaultdict(list)

    for key in candidates:
        task_index, window_index, _ = key
        task = tasks[task_index]
        corridor_id = window_meta[window_index]["corridor_id"]
        scheduled_date = window_meta[window_index]["date"]
        department_id = task.get("department_id")

        capacity = max(1, int(corridor_capacities.get(corridor_id, 1)))
        # Store the capacity separately because it belongs to the corridor.
        corridor_groups[(corridor_id, scheduled_date, department_id)].append(intervals[key])

    for (corridor_id, _scheduled_date, _department_id), group_intervals in corridor_groups.items():
        capacity = max(1, int(corridor_capacities.get(corridor_id, 1)))
        model.add_cumulative(
            group_intervals,
            [1] * len(group_intervals),
            capacity,
        )

    # ========================================================================
    # CONSTRAINT 5 — EXACT BLOCK COUNT VARIABLES
    # ========================================================================

    # A selected candidate starts a new physical block iff it does not
    # overlap any earlier selected candidate on the same corridor/date.
    # This counts connected overlap components exactly, including chains:
    # A overlaps B, B overlaps C => one block even if A does not overlap C.
    block_start_vars: dict[tuple[int, int, int], cp_model.IntVar] = {}

    ordered_candidates = sorted(
        candidates.keys(),
        key=lambda k: (k[1], k[2], k[0]),
    )

    for position, key in enumerate(ordered_candidates):
        task_index, window_index, start = key
        task = tasks[task_index]
        end = start + int(task["estimated_duration_minutes"])
        corridor_id = window_meta[window_index]["corridor_id"]
        scheduled_date = window_meta[window_index]["date"]

        block_start = model.new_bool_var(
            f"block_start_t{task_index}_w{window_index}_s{start}"
        )
        block_start_vars[key] = block_start

        # A block cannot start unless this candidate is selected.
        model.add(block_start <= assign[key])

        predecessors: list[cp_model.IntVar] = []
        for previous_key in ordered_candidates[:position]:
            p_task_idx, p_window_idx, p_start = previous_key
            p_task = tasks[p_task_idx]
            p_end = p_start + int(p_task["estimated_duration_minutes"])
            p_corridor = window_meta[p_window_idx]["corridor_id"]
            p_date = window_meta[p_window_idx]["date"]

            if p_corridor != corridor_id or p_date != scheduled_date:
                continue
            if not _overlaps(start, end, p_start, p_end):
                continue

            predecessors.append(assign[previous_key])
            # If an earlier overlapping candidate is selected, this cannot
            # be the first candidate of the block.
            model.add(block_start + assign[previous_key] <= 1)

        if predecessors:
            # If selected and no predecessor is selected, block_start must be 1.
            model.add(
                block_start >= assign[key] - sum(predecessors)
            )
        else:
            # No predecessor can connect this candidate to an earlier block.
            model.add(block_start == assign[key])

    # ========================================================================
    # CONSTRAINT 6 — CROSS-DEPARTMENT COORDINATION VARIABLES
    # ========================================================================

    coordination_vars: list[cp_model.IntVar] = []

    for index_a in range(len(candidate_items)):
        key_a, var_a = candidate_items[index_a]
        task_a_idx, window_a_idx, start_a = key_a
        task_a = tasks[task_a_idx]
        end_a = start_a + int(task_a["estimated_duration_minutes"])

        for index_b in range(index_a + 1, len(candidate_items)):
            key_b, var_b = candidate_items[index_b]
            task_b_idx, window_b_idx, start_b = key_b
            if task_a_idx == task_b_idx:
                continue

            task_b = tasks[task_b_idx]
            if task_a.get("department_id") == task_b.get("department_id"):
                continue
            if window_meta[window_a_idx]["date"] != window_meta[window_b_idx]["date"]:
                continue
            if window_meta[window_a_idx]["corridor_id"] != window_meta[window_b_idx]["corridor_id"]:
                continue

            end_b = start_b + int(task_b["estimated_duration_minutes"])
            if not _overlaps(start_a, end_a, start_b, end_b):
                continue

            coord = model.new_bool_var(
                f"coord_t{task_a_idx}_t{task_b_idx}_w{window_a_idx}_s{start_a}"
            )
            model.add(coord <= var_a)
            model.add(coord <= var_b)
            model.add(coord >= var_a + var_b - 1)
            coordination_vars.append(coord)

    # ========================================================================
    # OBJECTIVE
    # ========================================================================

    objective_terms = []

    for key, variable in assign.items():
        task_index, window_index, _ = key
        task = tasks[task_index]
        window = window_meta[window_index]

        priority_score = _safe_float(task.get("priority_score"), 0.0)
        priority_component = int(round(priority_score * WEIGHT_PRIORITY))

        criticality_component = (
            _criticality_score(task.get("criticality", 5))
            * WEIGHT_CRITICALITY
        )

        asset_impact = max(
            0.0,
            min(100.0, _safe_float(task.get("asset_impact_score", 0))),
        )
        asset_component = int(round(asset_impact * WEIGHT_ASSET_IMPACT))

        overdue_component = (
            min(_overdue_days(task.get("due_date"), today), 30)
            * WEIGHT_OVERDUE
        )

        freight_component = _freight_penalty(window)
        duration = int(task.get("estimated_duration_minutes", 0))
        duration_penalty = duration * PENALTY_BLOCK_MINUTE

        candidate_score = (
            priority_component
            + criticality_component
            + asset_component
            + overdue_component
            - freight_component
            - duration_penalty
        )

        objective_terms.append(candidate_score * variable)

    # Reward cross-department coordination.
    for variable in coordination_vars:
        objective_terms.append(WEIGHT_COORDINATION * variable)

    # Explicitly penalize the exact number of physical block starts.
    for variable in block_start_vars.values():
        objective_terms.append(-PENALTY_BLOCK_COUNT * variable)

    if objective_terms:
        model.maximize(sum(objective_terms))

    # ========================================================================
    # SOLVE
    # ========================================================================

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVER_TIME_LIMIT_SECONDS
    solver.parameters.num_search_workers = SOLVER_WORKERS
    solver.parameters.log_search_progress = False

    status = solver.solve(model)
    solve_ms = (time.time() - solve_start) * 1000
    status_name = solver.status_name(status)

    if status == cp_model.MODEL_INVALID:
        raise ValueError(
            "CP-SAT model is invalid. This indicates a bug in constraint construction."
        )

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        reason_map = {
            cp_model.INFEASIBLE: "solver_infeasible",
            cp_model.UNKNOWN: "solver_timeout",
        }
        fallback_reason = reason_map.get(
            status,
            f"solver_{status_name.lower()}",
        )

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
                    "reason": fallback_reason,
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
                "solve_time_ms": round(solve_ms, 2),
                "objective": "no feasible solution found",
            },
        }

    # ========================================================================
    # EXTRACT SOLUTION
    # ========================================================================

    selected_candidates: list[tuple[int, int, int]] = []
    task_scheduled = {i: False for i in range(len(tasks))}

    for key, variable in assign.items():
        if solver.boolean_value(variable):
            task_index, _, _ = key
            task_scheduled[task_index] = True
            selected_candidates.append(key)

    # ========================================================================
    # BUILD BLOCK GROUPS
    # ========================================================================

    uf_parent: dict[tuple[int, int, int], tuple[int, int, int]] = {}

    def _uf_find(x: tuple[int, int, int]) -> tuple[int, int, int]:
        while uf_parent[x] != x:
            uf_parent[x] = uf_parent[uf_parent[x]]
            x = uf_parent[x]
        return x

    def _uf_union(a: tuple[int, int, int], b: tuple[int, int, int]) -> None:
        ra, rb = _uf_find(a), _uf_find(b)
        if ra != rb:
            uf_parent[ra] = rb

    for candidate in selected_candidates:
        uf_parent[candidate] = candidate

    for i in range(len(selected_candidates)):
        ci = selected_candidates[i]
        ti_idx, wi_idx, si = ci
        ti = tasks[ti_idx]
        ei = si + int(ti["estimated_duration_minutes"])
        ci_corridor = window_meta[wi_idx]["corridor_id"]
        ci_date = window_meta[wi_idx]["date"]

        for j in range(i + 1, len(selected_candidates)):
            cj = selected_candidates[j]
            tj_idx, wj_idx, sj = cj
            tj = tasks[tj_idx]
            ej = sj + int(tj["estimated_duration_minutes"])
            cj_corridor = window_meta[wj_idx]["corridor_id"]
            cj_date = window_meta[wj_idx]["date"]

            if (
                ci_corridor == cj_corridor
                and ci_date == cj_date
                and _overlaps(si, ei, sj, ej)
            ):
                _uf_union(ci, cj)

    uf_groups: dict[tuple[int, int, int], list[tuple[int, int, int]]] = defaultdict(list)
    for candidate in selected_candidates:
        uf_groups[_uf_find(candidate)].append(candidate)

    block_groups = list(uf_groups.values())

    # ========================================================================
    # CREATE SCHEDULED OUTPUT
    # ========================================================================

    block_group_lookup: dict[tuple[int, int, int], str] = {}
    total_block_minutes = 0
    coordinated_tasks = 0
    shared_blocks = 0

    for group_number, group in enumerate(block_groups, start=1):
        if not group:
            continue

        first_task_index, first_window_index, _ = group[0]
        first_window = window_meta[first_window_index]

        group_start = min(candidate[2] for candidate in group)
        group_end = max(
            candidate[2]
            + int(tasks[candidate[0]]["estimated_duration_minutes"])
            for candidate in group
        )
        group_duration = group_end - group_start
        total_block_minutes += group_duration

        departments = {
            tasks[candidate[0]].get("department_id")
            for candidate in group
        }
        if len(departments) > 1:
            shared_blocks += 1
            coordinated_tasks += len(group)

        block_group_id = (
            f"BG{str(horizon_str)[0].upper()}-"
            f"{first_window['id']}-{group_start}-{group_number}"
        )

        for candidate in group:
            block_group_lookup[candidate] = block_group_id

    scheduled: list[dict] = []

    for candidate in selected_candidates:
        task_index, window_index, start = candidate
        task = tasks[task_index]
        duration = int(task["estimated_duration_minutes"])
        end = start + duration

        scheduled.append(
            {
                "task_id": task["id"],
                "task_ref": task["task_ref"],
                "department_id": task["department_id"],
                "department_code": task["department_code"],
                "corridor_id": task["corridor_id"],
                "corridor_block_id": task["corridor_block_id"],
                "defect_type": task["defect_type"],
                "window_id": window_meta[window_index]["id"],
                "block_group_id": block_group_lookup[candidate],
                "scheduled_date": window_meta[window_index]["date"],
                "entry_time": _from_minutes(start),
                "exit_time": _from_minutes(end),
                "priority_score": task["priority_score"],
                "criticality": task["criticality"],
                "asset_impact_score": task["asset_impact_score"],
                "resource_id": task.get("required_resource_id"),
                "status": "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
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
            corridor_capacities=corridor_capacities,
            today=today,
        )

        unscheduled.append(
            {
                "task_id": task["id"],
                "task_ref": task["task_ref"],
                "department_code": task["department_code"],
                "corridor_block_id": task["corridor_block_id"],
                "defect_type": task["defect_type"],
                "criticality": task["criticality"],
                "due_date": task["due_date"],
                "priority_score": task["priority_score"],
                "reason": reason,
            }
        )

    # ========================================================================
    # KPI METRICS
    # ========================================================================

    total_window_minutes = sum(
        window["total_free_minutes"] for window in window_meta
    )

    if total_window_minutes > 0:
        utilization = total_block_minutes / total_window_minutes
        asset_availability_score = round(
            max(0.0, min(100.0, 100.0 * (1.0 - utilization))),
            2,
        )
    else:
        asset_availability_score = 100.0

    total_blocks = len(block_groups)

    average_scheduled_priority = (
        round(
            sum(entry["priority_score"] for entry in scheduled)
            / len(scheduled),
            2,
        )
        if scheduled
        else 0.0
    )

    overdue_tasks_scheduled = sum(
        1
        for candidate in selected_candidates
        if _overdue_days(tasks[candidate[0]].get("due_date"), today) > 0
    )

    return {
        "scheduled": scheduled,
        "unscheduled": unscheduled,
        "optimization_summary": {
            "solver_status": status_name,
            "solver_optimal": status == cp_model.OPTIMAL,
            "solver_feasible": status in (cp_model.OPTIMAL, cp_model.FEASIBLE),
            "total_scheduled": len(scheduled),
            "total_unscheduled": len(unscheduled),
            "total_blocks": total_blocks,
            "total_block_minutes": total_block_minutes,
            "asset_availability_score": asset_availability_score,
            "coordinated_tasks": coordinated_tasks,
            "shared_blocks": shared_blocks,
            "overdue_tasks_scheduled": overdue_tasks_scheduled,
            "average_scheduled_priority": average_scheduled_priority,
            "solve_time_ms": round(solve_ms, 2),
            "objective": (
                "priority + criticality + asset impact + overdue completion "
                "+ cross-department coordination - exact block count "
                "- block duration - freight impact"
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
    resource_available: dict[Any, bool],
    corridor_capacities: dict[int, int],
    today: date,
) -> str:
    """Return a best-effort human-readable reason."""

    try:
        duration = int(task.get("estimated_duration_minutes", 0))
    except (TypeError, ValueError):
        duration = 0

    if duration <= 0:
        return "invalid_duration"

    resource_id = task.get("required_resource_id")
    if resource_id is not None and not resource_available.get(resource_id, False):
        return "resource_unavailable"

    task_candidates = [
        candidate for candidate in candidates if candidate[0] == task_index
    ]
    if not task_candidates:
        return "no_feasible_time_window"

    task_defect = str(task.get("defect_type", "")).upper()
    capacity_seen = False

    selected_by_task = {candidate[0] for candidate in selected_candidates}

    for _, window_index, start in task_candidates:
        end = start + duration
        window = window_meta[window_index]

        for selected in selected_candidates:
            other_index = selected[0]
            if other_index == task_index or other_index not in selected_by_task:
                continue

            other_task = tasks[other_index]
            other_window = window_meta[selected[1]]

            if other_window["date"] != window["date"]:
                continue
            if other_window["corridor_id"] != window["corridor_id"]:
                continue

            other_start = selected[2]
            other_end = other_start + int(other_task["estimated_duration_minutes"])
            if not _overlaps(start, end, other_start, other_end):
                continue

            if (
                resource_id is not None
                and other_task.get("required_resource_id") == resource_id
            ):
                return "resource_conflict"

            other_defect = str(other_task.get("defect_type", "")).upper()
            if frozenset((task_defect, other_defect)) in normalized_incompatible:
                return "safety_incompatibility"

            if task.get("department_id") == other_task.get("department_id"):
                corr_id = window["corridor_id"]
                capacity = max(1, int(corridor_capacities.get(corr_id, 1)))
                capacity_seen = True

                # Count all selected same-department tasks overlapping this
                # candidate, not merely the first one encountered.
                simultaneous = 1
                for selected_2 in selected_candidates:
                    if selected_2[0] in (task_index, other_index):
                        continue
                    other_2 = tasks[selected_2[0]]
                    window_2 = window_meta[selected_2[1]]
                    if window_2["date"] != window["date"]:
                        continue
                    if window_2["corridor_id"] != window["corridor_id"]:
                        continue
                    if other_2.get("department_id") != task.get("department_id"):
                        continue
                    s2 = selected_2[2]
                    e2 = s2 + int(other_2["estimated_duration_minutes"])
                    if _overlaps(start, end, s2, e2):
                        simultaneous += 1

                if simultaneous >= capacity:
                    return "corridor_capacity_exhausted"

            if (
                _safe_float(other_task.get("priority_score"))
                > _safe_float(task.get("priority_score"))
            ):
                return "higher_priority_task_preempted"

    if capacity_seen:
        return "corridor_capacity_exhausted"

    if _overdue_days(task.get("due_date"), today) > 0:
        return "optimizer_tradeoff_overdue_task"

    return "optimizer_tradeoff_lower_priority"


# ============================================================================
# EMPTY RESULT
# ============================================================================

def _empty_result(reason: str, horizon_str: str, solve_start: float) -> dict:
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
            "solve_time_ms": round((time.time() - solve_start) * 1000, 2),
            "objective": "no optimization performed",
        },
    }
