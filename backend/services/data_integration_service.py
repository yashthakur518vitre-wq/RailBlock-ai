"""
services/data_integration_service.py — Mock data integration adapters.

SIH26027 context:
  Real Indian Railways maintenance data lives in separate systems:
    - TMS  (Engineering/Track Maintenance System)
    - SMMS (Signal Maintenance Management System)
    - TDMS (Traction Distribution Management System)
    - COA  (Control Office Application — corridor availability + train timetable)

  This service provides a clean adapter architecture that:
    1. Accepts mock payloads mimicking each source system's data format.
    2. Normalises them into the common MaintenanceTask / AvailabilityWindow
       database format.
    3. Persists the normalised records.

  For the SIH prototype these are JSON adapters.
  Future integration can replace the adapter bodies with real API/DB calls
  without changing the normalisation or persistence logic.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _require_corridor(block_id: str, db: Session) -> models.CorridorModel:
    """Fetch a corridor by block_id or raise 404."""
    corridor = (
        db.query(models.CorridorModel)
        .filter(models.CorridorModel.block_id == block_id.upper())
        .first()
    )
    if not corridor:
        raise HTTPException(
            status_code=404,
            detail=f"Corridor '{block_id}' not found. Add it first via POST /corridors."
        )
    return corridor


def _require_department(code: str, db: Session) -> models.DepartmentModel:
    """Fetch a department by code or raise 404."""
    dept = (
        db.query(models.DepartmentModel)
        .filter(models.DepartmentModel.code == code.upper())
        .first()
    )
    if not dept:
        raise HTTPException(
            status_code=404,
            detail=f"Department '{code}' not found. Add it first via POST /departments."
        )
    return dept


def _check_duplicate_task(task_ref: str, db: Session) -> bool:
    """Return True if the task_ref already exists in the database."""
    return (
        db.query(models.MaintenanceTaskModel)
        .filter(models.MaintenanceTaskModel.task_ref == task_ref)
        .first()
        is not None
    )


# ---------------------------------------------------------------------------
# TMS Adapter — Engineering / Track Maintenance System
# ---------------------------------------------------------------------------

def import_tms_tasks(payloads: list[dict], db: Session) -> dict:
    """
    Normalise TMS (Engineering) task payloads into MaintenanceTask records.

    TMS field mapping:
        tms_ref           → task_ref  (prefixed with "ENG-" for clarity)
        section_id        → corridor_block_id
        defect_code       → defect_type (uppercased)
        severity          → criticality (1–5)
        logged_date       → reported_date
        target_date       → due_date
        work_hours * 60   → estimated_duration_minutes
        track_impact_index → asset_impact_score

    Source system: TMS → Department code: ENG
    """
    dept = _require_department("ENG", db)
    imported, skipped = [], []

    for payload in payloads:
        task_ref = f"ENG-{payload['tms_ref']}"

        if _check_duplicate_task(task_ref, db):
            skipped.append({"task_ref": task_ref, "reason": "duplicate"})
            continue

        corridor = _require_corridor(payload["section_id"], db)

        duration_min = max(1, int(round(float(payload["work_hours"]) * 60)))

        new_task = models.MaintenanceTaskModel(
            task_ref=task_ref,
            department_id=dept.id,
            corridor_id=corridor.id,
            description=payload.get("defect_description", f"TMS task {task_ref}"),
            defect_type=str(payload["defect_code"]).upper().replace(" ", "_"),
            criticality=int(payload["severity"]),
            reported_date=_parse_date(payload["logged_date"]),
            due_date=_parse_date(payload["target_date"]),
            estimated_duration_minutes=duration_min,
            asset_impact_score=float(payload.get("track_impact_index", 0.0)),
            status="pending",
        )
        db.add(new_task)
        imported.append(task_ref)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")

    return {
        "source": "TMS",
        "department": "ENG",
        "imported": len(imported),
        "skipped": len(skipped),
        "task_refs": imported,
        "skipped_details": skipped,
    }


# ---------------------------------------------------------------------------
# SMMS Adapter — Signal & Telecom Maintenance Management System
# ---------------------------------------------------------------------------

def import_smms_tasks(payloads: list[dict], db: Session) -> dict:
    """
    Normalise SMMS (Signal & Telecom) task payloads into MaintenanceTask records.

    SMMS field mapping:
        smms_ref             → task_ref (prefixed "SNT-")
        location_id          → corridor_block_id
        fault_type           → defect_type
        priority_level       → criticality
        reported_on          → reported_date
        due_on               → due_date
        est_duration_hrs * 60 → estimated_duration_minutes
        signal_impact_score  → asset_impact_score

    Source system: SMMS → Department code: SNT
    """
    dept = _require_department("SNT", db)
    imported, skipped = [], []

    for payload in payloads:
        task_ref = f"SNT-{payload['smms_ref']}"

        if _check_duplicate_task(task_ref, db):
            skipped.append({"task_ref": task_ref, "reason": "duplicate"})
            continue

        corridor = _require_corridor(payload["location_id"], db)
        duration_min = max(1, int(round(float(payload["est_duration_hrs"]) * 60)))

        new_task = models.MaintenanceTaskModel(
            task_ref=task_ref,
            department_id=dept.id,
            corridor_id=corridor.id,
            description=payload.get("fault_description", f"SMMS task {task_ref}"),
            defect_type=str(payload["fault_type"]).upper().replace(" ", "_"),
            criticality=int(payload["priority_level"]),
            reported_date=_parse_date(payload["reported_on"]),
            due_date=_parse_date(payload["due_on"]),
            estimated_duration_minutes=duration_min,
            asset_impact_score=float(payload.get("signal_impact_score", 0.0)),
            status="pending",
        )
        db.add(new_task)
        imported.append(task_ref)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")

    return {
        "source": "SMMS",
        "department": "SNT",
        "imported": len(imported),
        "skipped": len(skipped),
        "task_refs": imported,
        "skipped_details": skipped,
    }


# ---------------------------------------------------------------------------
# TDMS Adapter — Traction Distribution Management System
# ---------------------------------------------------------------------------

def import_tdms_tasks(payloads: list[dict], db: Session) -> dict:
    """
    Normalise TDMS (Traction Distribution / OHE) task payloads.

    TDMS field mapping:
        tdms_ref             → task_ref (prefixed "TD-")
        ohe_section          → corridor_block_id
        issue_type           → defect_type
        urgency              → criticality
        date_reported        → reported_date
        completion_deadline  → due_date
        duration_minutes     → estimated_duration_minutes (direct)
        traction_impact      → asset_impact_score

    Source system: TDMS → Department code: TD
    """
    dept = _require_department("TD", db)
    imported, skipped = [], []

    for payload in payloads:
        task_ref = f"TD-{payload['tdms_ref']}"

        if _check_duplicate_task(task_ref, db):
            skipped.append({"task_ref": task_ref, "reason": "duplicate"})
            continue

        corridor = _require_corridor(payload["ohe_section"], db)
        duration_min = max(1, int(payload["duration_minutes"]))

        new_task = models.MaintenanceTaskModel(
            task_ref=task_ref,
            department_id=dept.id,
            corridor_id=corridor.id,
            description=payload.get("issue_description", f"TDMS task {task_ref}"),
            defect_type=str(payload["issue_type"]).upper().replace(" ", "_"),
            criticality=int(payload["urgency"]),
            reported_date=_parse_date(payload["date_reported"]),
            due_date=_parse_date(payload["completion_deadline"]),
            estimated_duration_minutes=duration_min,
            asset_impact_score=float(payload.get("traction_impact", 0.0)),
            status="pending",
        )
        db.add(new_task)
        imported.append(task_ref)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")

    return {
        "source": "TDMS",
        "department": "TD",
        "imported": len(imported),
        "skipped": len(skipped),
        "task_refs": imported,
        "skipped_details": skipped,
    }


# ---------------------------------------------------------------------------
# COA Adapter — Control Office Application
# Imports availability windows AND train occupancy records
# ---------------------------------------------------------------------------

def import_coa_windows(payloads: list[dict], db: Session) -> dict:
    """
    Import availability windows from a mock COA payload.

    COA field mapping:
        block_section   → corridor_block_id
        window_date     → date
        from_time       → start_time
        to_time         → end_time
        goods_train_clear → is_goods_forecast_clear
    """
    imported = 0

    for payload in payloads:
        corridor = _require_corridor(payload["block_section"], db)

        new_window = models.AvailabilityWindowModel(
            corridor_id=corridor.id,
            date=_parse_date(payload["window_date"]),
            start_time=payload["from_time"],
            end_time=payload["to_time"],
            is_goods_forecast_clear=bool(payload.get("goods_train_clear", True)),
        )
        db.add(new_window)
        imported += 1

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")

    return {"source": "COA", "type": "availability_windows", "imported": imported}


def import_coa_occupancy(payloads: list[dict], db: Session) -> dict:
    """
    Import train occupancy records from a mock COA payload.

    COA field mapping:
        train_number   → train_id (must exist in trains table)
        block_section  → corridor_block_id
        occupancy_date → date
        arrival_time   → entry_time
        departure_time → exit_time
        train_type     → source ("goods" → "goods_forecast", else "timetable")
    """
    imported = 0

    for payload in payloads:
        # Resolve train
        train = (
            db.query(models.TrainModel)
            .filter(models.TrainModel.train_id == str(payload["train_number"]))
            .first()
        )
        if not train:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Train '{payload['train_number']}' not found. "
                    "Add it first via POST /trains."
                )
            )

        corridor = _require_corridor(payload["block_section"], db)

        # Map train_type to source value
        train_type = str(payload.get("train_type", "passenger")).lower()
        source = "goods_forecast" if train_type == "goods" else "timetable"

        new_occ = models.TrainOccupancyModel(
            train_id=train.id,
            corridor_id=corridor.id,
            date=_parse_date(payload["occupancy_date"]),
            entry_time=payload["arrival_time"],
            exit_time=payload["departure_time"],
            source=source,
        )
        db.add(new_occ)
        imported += 1

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")

    return {"source": "COA", "type": "train_occupancy", "imported": imported}


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _parse_date(value) -> date:
    """Accept a date object or an ISO 8601 string and return a date object."""
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format '{value}'. Expected YYYY-MM-DD."
        )
