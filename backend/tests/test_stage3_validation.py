from datetime import date

from optimizer.cp_sat_optimizer import run_cp_sat_block_planning


TODAY = date(2026, 8, 28)


def make_task(
    task_id,
    task_ref,
    department_id,
    department_code,
    corridor_id=1,
    defect_type="TRACK",
    duration=120,
    priority=50,
    resource_id=None,
):
    return {
        "id": task_id,
        "task_ref": task_ref,
        "department_id": department_id,
        "department_code": department_code,
        "corridor_id": corridor_id,
        "corridor_block_id": f"B{corridor_id}",
        "defect_type": defect_type,
        "estimated_duration_minutes": duration,
        "priority_score": priority,
        "required_resource_id": resource_id,
        "due_date": TODAY,
        "criticality": 3,
        "asset_impact_score": 50,
    }


def make_window(
    window_id,
    start,
    end,
    corridor_id=1,
    window_date=TODAY,
):
    return {
        "id": window_id,
        "corridor_id": corridor_id,
        "corridor_block_id": f"B{corridor_id}",
        "date": window_date,
        "start_time": start,
        "end_time": end,
        "is_goods_forecast_clear": True,
    }


def run_test(
    name,
    tasks,
    windows,
    occupancies=None,
    incompatible_pairs=None,
    resources=None,
    capacities=None,
):
    print("\n" + "=" * 70)
    print(name)
    print("=" * 70)

    result = run_cp_sat_block_planning(
        tasks=tasks,
        windows=windows,
        train_occupancies=occupancies or [],
        incompatible_pairs=incompatible_pairs or set(),
        resources=resources or [],
        corridor_capacities=capacities or {1: 1},
        today=TODAY,
        horizon_str="weekly",
    )

    print("Solver:", result["optimization_summary"]["solver_status"])
    print("Scheduled:", result["optimization_summary"]["total_scheduled"])
    print("Unscheduled:", result["optimization_summary"]["total_unscheduled"])

    for item in result["scheduled"]:
        print(
            f"  SCHEDULED: {item['task_ref']} "
            f"{item['entry_time']}-{item['exit_time']} "
            f"group={item['block_group_id']}"
        )

    for item in result["unscheduled"]:
        print(
            f"  UNSCHEDULED: {item['task_ref']} "
            f"reason={item['reason']}"
        )

    return result


# ======================================================================
# TEST 1 — RESOURCE COLLISION
# ======================================================================

def test_resource_collision():

    tasks = [
        make_task(
            1, "TASK-A",
            1, "TRACK",
            duration=120,
            priority=90,
            resource_id=1,
        ),
        make_task(
            2, "TASK-B",
            1, "TRACK",
            duration=120,
            priority=80,
            resource_id=1,
        ),
    ]

    windows = [
        make_window(1, "01:00", "04:00")
    ]

    resources = [
        {
            "id": 1,
            "resource_code": "RESOURCE-1",
            "availability_status": "available",
        }
    ]

    result = run_test(
        "TEST 1 — RESOURCE COLLISION",
        tasks,
        windows,
        resources=resources,
        capacities={1: 2},
    )

    scheduled = result["scheduled"]

    # Both tasks may be scheduled only if they are non-overlapping.
    assert len(scheduled) == 2

    a = next(x for x in scheduled if x["task_ref"] == "TASK-A")
    b = next(x for x in scheduled if x["task_ref"] == "TASK-B")

    assert not (
        a["entry_time"] < b["exit_time"]
        and b["entry_time"] < a["exit_time"]
    )

    print("PASS")


# ======================================================================
# TEST 2 — CORRIDOR CAPACITY
# ======================================================================

def test_corridor_capacity():

    tasks = [
        make_task(
            1, "TASK-A",
            1, "TRACK",
            duration=120,
            priority=95,
        ),
        make_task(
            2, "TASK-B",
            1, "TRACK",
            duration=120,
            priority=90,
        ),
        make_task(
            3, "TASK-C",
            1, "TRACK",
            duration=120,
            priority=85,
        ),
    ]

    windows = [
        make_window(1, "01:00", "04:00")
    ]

    result = run_test(
        "TEST 2 — CORRIDOR CAPACITY = 2",
        tasks,
        windows,
        capacities={1: 2},
    )

    scheduled = result["scheduled"]

    # At most TWO tasks may overlap at any point.
    for i in range(len(scheduled)):
        for j in range(i + 1, len(scheduled)):
            for k in range(j + 1, len(scheduled)):

                a = scheduled[i]
                b = scheduled[j]
                c = scheduled[k]

                # If all three overlap, capacity=2 has been violated.
                if (
                    a["entry_time"] < b["exit_time"]
                    and b["entry_time"] < a["exit_time"]
                    and a["entry_time"] < c["exit_time"]
                    and c["entry_time"] < a["exit_time"]
                    and b["entry_time"] < c["exit_time"]
                    and c["entry_time"] < b["exit_time"]
                ):
                    raise AssertionError(
                        "Three tasks overlap simultaneously "
                        "despite corridor capacity=2"
                    )

    print("PASS")


# ======================================================================
# TEST 3 — SAFETY INCOMPATIBILITY
# ======================================================================

def test_safety_incompatibility():

    tasks = [
        make_task(
            1,
            "TRACK-A",
            1,
            "TRACK",
            defect_type="TRACK",
            duration=120,
            priority=90,
        ),
        make_task(
            2,
            "ELECTRICAL-A",
            2,
            "ELECTRICAL",
            defect_type="ELECTRICAL",
            duration=120,
            priority=80,
        ),
    ]

    windows = [
        make_window(1, "01:00", "04:00")
    ]

    result = run_test(
        "TEST 3 — SAFETY INCOMPATIBILITY",
        tasks,
        windows,
        incompatible_pairs={
            ("TRACK", "ELECTRICAL")
        },
        capacities={1: 2},
    )

    scheduled = result["scheduled"]

    assert len(scheduled) <= 2

    if len(scheduled) == 2:

        a = scheduled[0]
        b = scheduled[1]

        assert not (
            a["entry_time"] < b["exit_time"]
            and b["entry_time"] < a["exit_time"]
        )

    print("PASS")


# ======================================================================
# TEST 4 — CROSS-DEPARTMENT COORDINATION
# ======================================================================

def test_cross_department_coordination():

    tasks = [
        make_task(
            1,
            "TRACK-A",
            1,
            "TRACK",
            duration=120,
            priority=90,
        ),
        make_task(
            2,
            "SIGNAL-A",
            2,
            "SIGNAL",
            duration=60,
            priority=80,
        ),
        make_task(
            3,
            "ELECTRICAL-A",
            3,
            "ELECTRICAL",
            duration=120,
            priority=70,
        ),
    ]

    windows = [
        make_window(1, "02:00", "05:00")
    ]

    result = run_test(
        "TEST 4 — CROSS-DEPARTMENT COORDINATION",
        tasks,
        windows,
        capacities={1: 1},
    )

    scheduled = result["scheduled"]

    assert len(scheduled) == 3

    assert result["optimization_summary"]["shared_blocks"] == 1

    assert result["optimization_summary"]["coordinated_tasks"] == 3

    print("PASS")


# ======================================================================
# TEST 5 — OVERNIGHT TRAIN
# ======================================================================

def test_overnight_train():

    previous_day = date(2026, 8, 27)

    tasks = [
        make_task(
            1,
            "OVERNIGHT-MAINTENANCE",
            1,
            "TRACK",
            duration=30,
            priority=90,
        )
    ]

    windows = [
        make_window(
            1,
            "00:30",
            "01:00",
            window_date=TODAY,
        )
    ]

    occupancies = [
        {
            "corridor_id": 1,
            "date": previous_day,
            "entry_time": "23:30",
            "exit_time": "01:30",
            "source": "timetable",
        }
    ]

    result = run_test(
        "TEST 5 — OVERNIGHT TRAIN",
        tasks,
        windows,
        occupancies=occupancies,
        capacities={1: 1},
    )

    scheduled = result["scheduled"]

    assert len(scheduled) == 0, (
        "Maintenance was incorrectly scheduled "
        "during an overnight train occupancy."
    )

    print("PASS")


# ======================================================================
# TEST 6 — PRIORITY TRADE-OFF
# ======================================================================

def test_priority_tradeoff():

    tasks = [
        make_task(
            1,
            "HIGH-PRIORITY",
            1,
            "TRACK",
            duration=120,
            priority=95,
        ),
        make_task(
            2,
            "LOW-PRIORITY",
            1,
            "TRACK",
            duration=120,
            priority=40,
        ),
    ]

    windows = [
        make_window(1, "01:00", "03:00")
    ]

    result = run_test(
        "TEST 6 — PRIORITY TRADE-OFF",
        tasks,
        windows,
        capacities={1: 1},
    )

    scheduled = result["scheduled"]

    assert len(scheduled) == 1

    assert scheduled[0]["task_ref"] == "HIGH-PRIORITY"

    print("PASS")


# ======================================================================
# RUN ALL
# ======================================================================

if __name__ == "__main__":

    tests = [
        test_resource_collision,
        test_corridor_capacity,
        test_safety_incompatibility,
        test_cross_department_coordination,
        test_overnight_train,
        test_priority_tradeoff,
    ]

    passed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as exc:
            print("FAIL")
            print("Reason:", exc)

    print("\n" + "=" * 70)
    print(f"STAGE 3 VALIDATION: {passed}/{len(tests)} PASSED")
    print("=" * 70)

    if passed != len(tests):
        raise SystemExit(1)