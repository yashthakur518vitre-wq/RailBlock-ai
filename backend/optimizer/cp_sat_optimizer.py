"""
optimizer/cp_sat_optimizer.py — Google OR-Tools CP-SAT block planning engine.

This is the AI optimization core of RailBlock AI (SIH26027).

What this optimizer does:
  Given pending maintenance tasks, available corridor windows, train occupancy
  data and safety rules, it finds the optimal assignment of tasks to windows
  such that:

  - High-priority tasks are scheduled preferentially
  - Maintenance never overlaps with train operations (hard constraint)
  - The same physical resource (machine/crew) is never double-booked
  - Safety-incompatible task types never share a corridor block
  - Tasks from different departments are coordinated into shared blocks
    where safe (reducing total corridor downtime)

NOTE on terminology:
  CP-SAT = Constraint Programming - Satisfiability
  This is an exact combinatorial optimizer, not a machine learning model.
  The system is correctly described as "AI-powered intelligent optimization"
  because it uses an AI search strategy (DPLL with propagation + LP relaxation)
  to find provably optimal or near-optimal solutions.

References:
  - OR-Tools CP-SAT: https://developers.google.com/optimization/reference/python/sat/python/cp_model
  - Indian Railways block working: as per General Rules / Block Working manual
"""

from __future__ import annotations

import time
from datetime import date
from typing import Optional

from ortools.sat.python import cp_model


# ---------------------------------------------------------------------------
# Time helpers (self-contained so the optimizer has no external dependencies)
# ---------------------------------------------------------------------------

def _to_minutes(time_str: str) -> int:
    """Convert HH:MM string to total minutes since midnight."""
    h, m = map(int, time_str.split(":"))
    return h * 60 + m


def _from_minutes(total_minutes: int) -> str:
    """Convert total minutes since midnight to HH:MM string."""
    h = total_minutes // 60
    m = total_minutes % 60
    return f"{h:02d}:{m:02d}"


def _compute_free_intervals(
    window_start: int,
    window_end: int,
    occupancy_ranges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """
    Compute free (unoccupied) intervals inside [window_start, window_end)
    after removing train occupancy ranges that overlap with the window.

    occupancy_ranges: list of (entry_minutes, exit_minutes) from TrainOccupancy.
    Returns a list of (free_start, free_end) tuples in ascending order.
    """
    # Clip and merge occupancy ranges that overlap with this window
    clipped: list[tuple[int, int]] = []
    for start, end in occupancy_ranges:
        overlap_start = max(start, window_start)
        overlap_end = min(end, window_end)
        if overlap_start < overlap_end:
            clipped.append((overlap_start, overlap_end))

    if not clipped:
        return [(window_start, window_end)]

    clipped.sort()

    free: list[tuple[int, int]] = []
    cursor = window_start

    for s, e in clipped:
        if s > cursor:
            free.append((cursor, s))
        cursor = max(cursor, e)

    if cursor < window_end:
        free.append((cursor, window_end))

    return free


def _max_contiguous_free(
    window_start: int,
    window_end: int,
    occupancy_ranges: list[tuple[int, int]],
) -> int:
    """Return the length (minutes) of the longest contiguous free interval."""
    intervals = _compute_free_intervals(window_start, window_end, occupancy_ranges)
    if not intervals:
        return 0
    return max(e - s for s, e in intervals)


def _find_first_fitting_interval(
    intervals: list[tuple[int, int]],
    required_minutes: int,
) -> Optional[tuple[int, int]]:
    """
    Return the first free interval that can accommodate required_minutes.
    Returns (start, end) of that interval, or None if nothing fits.
    """
    for s, e in intervals:
        if (e - s) >= required_minutes:
            return (s, e)
    return None


# ---------------------------------------------------------------------------
# Main optimizer function
# ---------------------------------------------------------------------------

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
    Run the CP-SAT block planning optimizer.

    Args:
        tasks: List of pending task dicts. Required keys:
            id, task_ref, department_id, department_code, corridor_id,
            corridor_block_id, defect_type, estimated_duration_minutes,
            priority_score, required_resource_id (Optional[int]),
            due_date, criticality, asset_impact_score

        windows: List of availability window dicts. Required keys:
            id, corridor_id, corridor_block_id, date (date object),
            start_time, end_time, is_goods_forecast_clear

        train_occupancies: List of occupancy dicts. Required keys:
            corridor_id, date (date object), entry_time, exit_time, source

        incompatible_pairs: Set of (defect_type_A, defect_type_B) tuples.
            Both orderings are treated as incompatible.
            Example: {("TRACK_TAMPING", "OHE_REPLACEMENT")}

        resources: List of resource dicts. Required keys:
            id, resource_code, availability_status

        today: Reference date for the planning run.
        horizon_str: "weekly" or "monthly".

    Returns:
        Dict with keys: scheduled, unscheduled, optimization_summary
    """
    solve_start = time.time()

    if not tasks:
        return _empty_result("no_tasks", horizon_str, solve_start)

    # -----------------------------------------------------------------------
    # Step 1: Pre-process windows
    # For each window, compute occupancy ranges and max contiguous free time.
    # -----------------------------------------------------------------------

    # Build occupancy lookup: (corridor_id, date) → list of (entry_min, exit_min)
    occupancy_lookup: dict[tuple[int, date], list[tuple[int, int]]] = {}
    for occ in train_occupancies:
        key = (occ["corridor_id"], occ["date"])
        occupancy_lookup.setdefault(key, []).append(
            (_to_minutes(occ["entry_time"]), _to_minutes(occ["exit_time"]))
        )

    # Enrich each window with pre-computed free time data
    window_meta: list[dict] = []
    for w in windows:
        w_start = _to_minutes(w["start_time"])
        w_end = _to_minutes(w["end_time"])
        occ_ranges = occupancy_lookup.get((w["corridor_id"], w["date"]), [])
        free_intervals = _compute_free_intervals(w_start, w_end, occ_ranges)
        max_free = max((e - s for s, e in free_intervals), default=0)
        total_free = sum(e - s for s, e in free_intervals)

        window_meta.append({
            **w,
            "w_start": w_start,
            "w_end": w_end,
            "free_intervals": free_intervals,
            "max_contiguous_free": max_free,
            "total_free_minutes": total_free,
        })

    # -----------------------------------------------------------------------
    # Step 2: Determine feasible (task, window) pairs.
    # A pair is feasible if:
    #   (a) task corridor matches window corridor
    #   (b) task duration ≤ max contiguous free time in window
    #   (c) window is goods-forecast clear (already filtered upstream,
    #       but double-checked here for safety)
    #   (d) the required resource (if any) is available
    # -----------------------------------------------------------------------

    # Build resource availability lookup
    resource_available: dict[int, bool] = {
        r["id"]: (r["availability_status"] == "available")
        for r in resources
    }

    feasible: dict[tuple[int, int], bool] = {}  # (task_idx, window_idx) → True
    task_infeasibility_reasons: dict[int, list[str]] = {i: [] for i in range(len(tasks))}

    for i, task in enumerate(tasks):
        has_matching_window = False
        fits_in_some_window = False

        for j, wmeta in enumerate(window_meta):
            # Corridor match
            if task["corridor_id"] != wmeta["corridor_id"]:
                continue
            has_matching_window = True

            # Goods forecast check
            if not wmeta.get("is_goods_forecast_clear", True):
                task_infeasibility_reasons[i].append("goods_forecast_conflict")
                continue

            # Duration fit check
            if task["estimated_duration_minutes"] > wmeta["max_contiguous_free"]:
                continue
            fits_in_some_window = True

            # Resource availability check
            rid = task.get("required_resource_id")
            if rid is not None and not resource_available.get(rid, True):
                task_infeasibility_reasons[i].append("resource_unavailable")
                continue

            feasible[(i, j)] = True

        # Track why a task might be entirely infeasible
        if not has_matching_window:
            task_infeasibility_reasons[i].append("no_availability_window")
        elif not fits_in_some_window:
            task_infeasibility_reasons[i].append("insufficient_window_duration")

    # -----------------------------------------------------------------------
    # Step 3: Build the CP-SAT model
    # -----------------------------------------------------------------------

    model = cp_model.CpModel()

    # Decision variable: assign[i][j] = 1 if task i is scheduled in window j
    assign: dict[tuple[int, int], cp_model.IntVar] = {}
    for (i, j) in feasible:
        assign[(i, j)] = model.new_bool_var(f"assign_t{i}_w{j}")

    # -------------------------------------------------------------------
    # Constraint 1: Each task is assigned to AT MOST one window
    # -------------------------------------------------------------------
    for i in range(len(tasks)):
        task_window_vars = [assign[(i, j)] for j in range(len(window_meta)) if (i, j) in assign]
        if task_window_vars:
            model.add(sum(task_window_vars) <= 1)

    # -------------------------------------------------------------------
    # Constraint 2: At most one task per department per window.
    # Indian Railways safety rule: each department can only field one
    # maintenance team in a corridor block at a time.
    # -------------------------------------------------------------------
    for j in range(len(window_meta)):
        # Group tasks by department for this window
        dept_vars: dict[int, list[cp_model.IntVar]] = {}
        for i, task in enumerate(tasks):
            if (i, j) in assign:
                d = task["department_id"]
                dept_vars.setdefault(d, []).append(assign[(i, j)])

        for d, vars_list in dept_vars.items():
            if len(vars_list) > 1:
                model.add(sum(vars_list) <= 1)

    # -------------------------------------------------------------------
    # Constraint 3: Resource conflict — the same machine/crew cannot be
    # assigned to two tasks in the same window (or overlapping windows
    # on the same date, simplified here to same window).
    # -------------------------------------------------------------------
    for j in range(len(window_meta)):
        resource_vars: dict[int, list[cp_model.IntVar]] = {}
        for i, task in enumerate(tasks):
            rid = task.get("required_resource_id")
            if rid is not None and (i, j) in assign:
                resource_vars.setdefault(rid, []).append(assign[(i, j)])

        for rid, vars_list in resource_vars.items():
            if len(vars_list) > 1:
                model.add(sum(vars_list) <= 1)

    # -------------------------------------------------------------------
    # Constraint 4: Safety incompatibility — certain defect types must
    # not share a corridor block.
    # Example: TRACK_TAMPING + OHE_REPLACEMENT cannot coexist safely.
    # The incompatible_pairs set is fully configurable.
    # -------------------------------------------------------------------
    # Build a normalized set for O(1) lookup (check both orderings)
    norm_incompatible: set[frozenset[str]] = {
        frozenset(pair) for pair in incompatible_pairs
    }

    if norm_incompatible:
        for j in range(len(window_meta)):
            for i1 in range(len(tasks)):
                if (i1, j) not in assign:
                    continue
                dt1 = tasks[i1]["defect_type"]
                for i2 in range(i1 + 1, len(tasks)):
                    if (i2, j) not in assign:
                        continue
                    dt2 = tasks[i2]["defect_type"]
                    if frozenset([dt1, dt2]) in norm_incompatible:
                        # These two tasks cannot be in the same window
                        model.add(assign[(i1, j)] + assign[(i2, j)] <= 1)

    # -------------------------------------------------------------------
    # Objective: maximise weighted task coverage
    # Priority score is a float; CP-SAT needs integer coefficients.
    # We scale by 100 and round to preserve two decimal places of precision.
    # -------------------------------------------------------------------
    objective_terms: list = []
    for (i, j), var in assign.items():
        score_int = int(round(tasks[i]["priority_score"] * 100))
        objective_terms.append(score_int * var)

    if objective_terms:
        model.maximize(sum(objective_terms))

    # -----------------------------------------------------------------------
    # Step 4: Solve
    # -----------------------------------------------------------------------

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 60.0   # hard limit for prototype
    solver.parameters.log_search_progress = False  # keep logs clean

    status = solver.solve(model)
    solve_ms = (time.time() - solve_start) * 1000

    status_name = solver.status_name(status)

    # -----------------------------------------------------------------------
    # Step 5: Extract solution
    # -----------------------------------------------------------------------

    scheduled: list[dict] = []
    unscheduled: list[dict] = []

    # Track which tasks were scheduled
    task_scheduled: dict[int, bool] = {i: False for i in range(len(tasks))}

    # Group tasks by window (for block group assignment and timing)
    window_task_groups: dict[int, list[int]] = {}

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for (i, j), var in assign.items():
            if solver.boolean_value(var):
                task_scheduled[i] = True
                window_task_groups.setdefault(j, []).append(i)

        # -------------------------------------------------------------------
        # Build scheduled entries — compute block timing for each window group
        # -------------------------------------------------------------------
        for j, task_indices in window_task_groups.items():
            wmeta = window_meta[j]

            # The block duration = max task duration (all tasks run in parallel)
            # within the same corridor block window
            block_duration = max(
                tasks[i]["estimated_duration_minutes"] for i in task_indices
            )

            # Find the first contiguous free interval that fits the block
            fit = _find_first_fitting_interval(
                wmeta["free_intervals"], block_duration
            )
            if fit is None:
                # Shouldn't happen (we pre-filtered), but handle defensively
                block_start = wmeta["w_start"]
                block_end = block_start + block_duration
            else:
                block_start = fit[0]
                block_end = block_start + block_duration

            # Block group ID format: "BGY{window_id}-{start_minutes}"
            # Y = first letter of horizon (W=weekly, M=monthly)
            horizon_letter = horizon_str[0].upper()
            block_group_id = (
                f"BG{horizon_letter}{wmeta['id']}-{block_start}"
            )

            for i in task_indices:
                task = tasks[i]
                scheduled.append({
                    "task_id": task["id"],
                    "task_ref": task["task_ref"],
                    "department_code": task["department_code"],
                    "corridor_block_id": task["corridor_block_id"],
                    "defect_type": task["defect_type"],
                    "window_id": wmeta["id"],
                    "block_group_id": block_group_id,
                    "scheduled_date": wmeta["date"],
                    "entry_time": _from_minutes(block_start),
                    "exit_time": _from_minutes(block_end),
                    "priority_score": task["priority_score"],
                    "resource_id": task.get("required_resource_id"),
                    "status": "OPTIMALLY_SCHEDULED",
                })

    # -----------------------------------------------------------------------
    # Step 6: Determine unscheduled tasks and reasons
    # -----------------------------------------------------------------------

    # Build a set of task defect types that ARE scheduled, keyed by window
    # (for safety incompatibility reason detection)
    scheduled_by_window: dict[int, set[str]] = {}
    for entry in scheduled:
        wid = entry["window_id"]
        scheduled_by_window.setdefault(wid, set()).add(entry["defect_type"])

    for i, task in enumerate(tasks):
        if task_scheduled[i]:
            continue

        # Determine the most informative reason
        infeasibility_reasons = task_infeasibility_reasons.get(i, [])

        if "no_availability_window" in infeasibility_reasons:
            reason = "no_availability_window"
        elif "insufficient_window_duration" in infeasibility_reasons:
            reason = "insufficient_window_duration"
        elif "goods_forecast_conflict" in infeasibility_reasons:
            reason = "goods_forecast_conflict"
        elif "resource_unavailable" in infeasibility_reasons:
            reason = "resource_conflict"
        else:
            # Task had feasible windows but was not selected by the optimizer.
            # Determine the most likely blocking reason from model constraints.
            reason = _diagnose_unscheduled_reason(
                i, task, tasks, window_meta, assign, norm_incompatible
            )

        unscheduled.append({
            "task_id": task["id"],
            "task_ref": task["task_ref"],
            "department_code": task["department_code"],
            "corridor_block_id": task["corridor_block_id"],
            "defect_type": task["defect_type"],
            "criticality": task["criticality"],
            "due_date": task["due_date"],
            "priority_score": task["priority_score"],
            "reason": reason,
        })

    # -----------------------------------------------------------------------
    # Step 7: Compute optimization summary metrics
    # -----------------------------------------------------------------------

    # Total block minutes = sum of block durations (max task duration per group)
    total_block_minutes = 0
    for j, task_indices in window_task_groups.items():
        total_block_minutes += max(
            tasks[i]["estimated_duration_minutes"] for i in task_indices
        )

    # Coordinated tasks = tasks that share a block group with another dept
    coordinated_count = 0
    for j, task_indices in window_task_groups.items():
        if len(task_indices) > 1:
            depts_in_group = {tasks[i]["department_id"] for i in task_indices}
            if len(depts_in_group) > 1:
                # Multiple departments sharing this block → all are coordinated
                coordinated_count += len(task_indices)

    # Asset availability score:
    # Total window minutes available vs. minutes consumed by maintenance
    total_window_minutes = sum(wmeta["total_free_minutes"] for wmeta in window_meta)
    if total_window_minutes > 0:
        asset_availability_score = round(
            100.0 * (1.0 - total_block_minutes / total_window_minutes), 2
        )
        asset_availability_score = max(0.0, min(100.0, asset_availability_score))
    else:
        asset_availability_score = 100.0

    return {
        "scheduled": scheduled,
        "unscheduled": unscheduled,
        "optimization_summary": {
            "solver_status": status_name,
            "total_scheduled": len(scheduled),
            "total_unscheduled": len(unscheduled),
            "total_block_minutes": total_block_minutes,
            "asset_availability_score": asset_availability_score,
            "coordinated_tasks": coordinated_count,
            "solve_time_ms": round(solve_ms, 2),
        },
    }


# ---------------------------------------------------------------------------
# Helper: diagnose why an optimizer-feasible task was not scheduled
# ---------------------------------------------------------------------------

def _diagnose_unscheduled_reason(
    task_idx: int,
    task: dict,
    all_tasks: list[dict],
    window_meta: list[dict],
    assign: dict[tuple[int, int], cp_model.IntVar],
    norm_incompatible: set[frozenset[str]],
) -> str:
    """
    Heuristically determine why a task with feasible windows was not scheduled.
    Called after solving — we inspect the structure (not the solver state)
    to produce a human-readable reason.
    """
    dt = task["defect_type"]
    task_dept = task["department_id"]
    task_res = task.get("required_resource_id")

    for j, wmeta in enumerate(window_meta):
        if (task_idx, j) not in assign:
            continue  # not feasible for this window

        # Check if a safety incompatibility exists with another task in this window
        for i2, other_task in enumerate(all_tasks):
            if i2 == task_idx:
                continue
            if (i2, j) not in assign:
                continue
            if frozenset([dt, other_task["defect_type"]]) in norm_incompatible:
                return "safety_incompatibility"

        # Check resource conflict in this window
        if task_res is not None:
            for i2, other_task in enumerate(all_tasks):
                if i2 == task_idx:
                    continue
                if (i2, j) not in assign:
                    continue
                if other_task.get("required_resource_id") == task_res:
                    return "resource_conflict"

        # Check department conflict in this window
        for i2, other_task in enumerate(all_tasks):
            if i2 == task_idx:
                continue
            if (i2, j) not in assign:
                continue
            if other_task["department_id"] == task_dept:
                # Same dept competes for this window
                return "lower_priority_department_preempted"

    # No specific conflict found — task lost out to higher-priority tasks
    return "lower_priority_preempted"


# ---------------------------------------------------------------------------
# Empty result helper
# ---------------------------------------------------------------------------

def _empty_result(reason: str, horizon_str: str, solve_start: float) -> dict:
    """Return a well-formed empty result when there is nothing to optimize."""
    return {
        "scheduled": [],
        "unscheduled": [],
        "optimization_summary": {
            "solver_status": reason,
            "total_scheduled": 0,
            "total_unscheduled": 0,
            "total_block_minutes": 0,
            "asset_availability_score": 100.0,
            "coordinated_tasks": 0,
            "solve_time_ms": round((time.time() - solve_start) * 1000, 2),
        },
    }
