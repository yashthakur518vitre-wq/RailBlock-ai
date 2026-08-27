"""
main.py — RailBlock AI FastAPI application (SIH26027).

Architecture:
  This file contains ONLY route definitions and API-level concerns.

  Business logic lives in:
    - services/priority_service.py
    - services/scheduling_service.py
    - services/data_integration_service.py
    - optimizer/cp_sat_optimizer.py

Stage 1 improvements:
  - Better health checks
  - Database connectivity check
  - Environment-based CORS configuration
  - Cleaner application configuration
  - Existing API routes preserved
"""

from __future__ import annotations

import os
from datetime import date
from typing import Optional

from fastapi import (
    FastAPI,
    HTTPException,
    Depends,
    Query,
    Body,
)
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import engine, Base, SessionLocal
import models
import schemas
from services import (
    priority_service,
    scheduling_service,
    data_integration_service,
)


# ===========================================================================
# Configuration
# ===========================================================================

APP_VERSION = "2.1.0"
PROBLEM_STATEMENT = "SIH26027"
OPTIMIZER_NAME = "Google OR-Tools CP-SAT"

# During local development:
#   FRONTEND_ORIGINS=http://localhost:5173
#
# Multiple origins can be separated using commas:
#   FRONTEND_ORIGINS=http://localhost:5173,http://localhost:3000
#
# "*" is retained as the development fallback so the current frontend does
# not unexpectedly stop working.
frontend_origins = os.getenv(
    "FRONTEND_ORIGINS",
    "*",
)

if frontend_origins.strip() == "*":
    allowed_origins = ["*"]
else:
    allowed_origins = [
        origin.strip()
        for origin in frontend_origins.split(",")
        if origin.strip()
    ]


# ===========================================================================
# Application initialisation
# ===========================================================================

app = FastAPI(
    title="RailBlock AI",
    description=(
        "AI-Powered Automatic Block Planning for Indian Railways (SIH26027). "
        "Integrates maintenance data from Engineering, Signal & Telecom, and "
        "Traction Distribution departments and generates optimized block "
        "schedules using Google OR-Tools CP-SAT constraint programming."
    ),
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)


# ===========================================================================
# Database initialisation
# ===========================================================================

# Development-friendly table creation.
#
# IMPORTANT:
#   create_all() creates missing tables but does NOT perform schema migrations.
#   Alembic/database migrations should be introduced before production
#   deployment.
Base.metadata.create_all(bind=engine)


# ===========================================================================
# CORS
# ===========================================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False if "*" in allowed_origins else True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===========================================================================
# Database dependency
# ===========================================================================

def get_db():
    """
    Provide one SQLAlchemy database session per request.

    The session is:
      - committed by individual service/route operations
      - rolled back if an exception occurs
      - always closed after the request
    """
    db = SessionLocal()

    try:
        yield db

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


# ===========================================================================
# Root / Health
# ===========================================================================

@app.get(
    "/",
    tags=["Health"],
    summary="API information",
)
def read_root():
    return {
        "message": "RailBlock AI backend is running!",
        "version": APP_VERSION,
        "problem_statement": PROBLEM_STATEMENT,
        "optimizer": OPTIMIZER_NAME,
        "docs": "/docs",
    }


@app.get(
    "/health",
    tags=["Health"],
    summary="Basic application health check",
)
def health_check():
    """
    Lightweight health endpoint.

    This endpoint only confirms that the FastAPI application is running.
    """
    return {
        "status": "healthy",
        "service": "railblock-ai-backend",
        "version": APP_VERSION,
    }


@app.get(
    "/status",
    tags=["Health"],
    summary="Detailed application and database status",
)
def get_status(db: Session = Depends(get_db)):
    """
    Detailed health check.

    Unlike a static status response, this endpoint verifies that the
    application can actually communicate with the database.
    """

    database_status = "connected"

    try:
        db.execute(text("SELECT 1"))

    except Exception:
        database_status = "unavailable"

    overall_status = (
        "online"
        if database_status == "connected"
        else "degraded"
    )

    return {
        "status": overall_status,
        "project": "RailBlock AI",
        "problem_statement": PROBLEM_STATEMENT,
        "version": APP_VERSION,
        "optimizer": OPTIMIZER_NAME,
        "database": database_status,
    }


# ===========================================================================
# Departments
# ===========================================================================

@app.post(
    "/departments",
    tags=["Departments"],
    summary="Add a department (ENG / SNT / TD)",
    status_code=201,
)
def add_department(
    department: schemas.DepartmentCreate,
    db: Session = Depends(get_db),
):
    existing = (
        db.query(models.DepartmentModel)
        .filter(models.DepartmentModel.code == department.code)
        .first()
    )

    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Department '{department.code}' already exists.",
        )

    new_dept = models.DepartmentModel(
        code=department.code,
        name=department.name,
        source_system=department.source_system,
    )

    db.add(new_dept)
    db.commit()
    db.refresh(new_dept)

    return {
        "message": "Department added successfully.",
        "department": {
            "id": new_dept.id,
            "code": new_dept.code,
            "name": new_dept.name,
            "source_system": new_dept.source_system,
        },
    }


@app.get(
    "/departments",
    tags=["Departments"],
    summary="List all departments",
)
def get_departments(
    db: Session = Depends(get_db),
):
    departments = (
        db.query(models.DepartmentModel)
        .order_by(models.DepartmentModel.code)
        .all()
    )

    return {
        "count": len(departments),
        "departments": [
            {
                "id": d.id,
                "code": d.code,
                "name": d.name,
                "source_system": d.source_system,
            }
            for d in departments
        ],
    }


# ===========================================================================
# Corridors
# ===========================================================================

@app.post(
    "/corridors",
    tags=["Corridors"],
    summary="Add a corridor / railway block section",
    status_code=201,
)
def add_corridor(
    corridor: schemas.CorridorCreate,
    db: Session = Depends(get_db),
):
    block_id = corridor.block_id.strip().upper()

    existing = (
        db.query(models.CorridorModel)
        .filter(models.CorridorModel.block_id == block_id)
        .first()
    )

    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Corridor '{block_id}' already exists.",
        )

    new_corridor = models.CorridorModel(
        block_id=block_id,
        block_name=corridor.block_name.strip(),
        start_station=corridor.start_station.strip(),
        end_station=corridor.end_station.strip(),
        line_capacity_per_day=corridor.line_capacity_per_day,
        annual_gmt=corridor.annual_gmt,
    )

    db.add(new_corridor)
    db.commit()
    db.refresh(new_corridor)

    return {
        "message": "Corridor added successfully.",
        "corridor": {
            "id": new_corridor.id,
            "block_id": new_corridor.block_id,
            "block_name": new_corridor.block_name,
            "start_station": new_corridor.start_station,
            "end_station": new_corridor.end_station,
            "line_capacity_per_day": new_corridor.line_capacity_per_day,
            "annual_gmt": new_corridor.annual_gmt,
        },
    }


@app.get(
    "/corridors",
    tags=["Corridors"],
    summary="List all corridors",
)
def get_corridors(
    db: Session = Depends(get_db),
):
    corridors = (
        db.query(models.CorridorModel)
        .order_by(models.CorridorModel.block_id)
        .all()
    )

    return {
        "count": len(corridors),
        "corridors": [
            {
                "id": c.id,
                "block_id": c.block_id,
                "block_name": c.block_name,
                "start_station": c.start_station,
                "end_station": c.end_station,
                "line_capacity_per_day": c.line_capacity_per_day,
                "annual_gmt": c.annual_gmt,
            }
            for c in corridors
        ],
    }


# ===========================================================================
# Resources
# ===========================================================================

@app.post(
    "/resources",
    tags=["Resources"],
    summary="Add a maintenance resource (machine or crew)",
    status_code=201,
)
def add_resource(
    resource: schemas.ResourceCreate,
    db: Session = Depends(get_db),
):
    resource_code = resource.resource_code.strip().upper()

    existing = (
        db.query(models.ResourceModel)
        .filter(
            models.ResourceModel.resource_code == resource_code
        )
        .first()
    )

    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Resource '{resource_code}' already exists.",
        )

    department_code = resource.department_code.strip().upper()

    dept = (
        db.query(models.DepartmentModel)
        .filter(
            models.DepartmentModel.code == department_code
        )
        .first()
    )

    if not dept:
        raise HTTPException(
            status_code=404,
            detail=f"Department '{department_code}' not found.",
        )

    new_resource = models.ResourceModel(
        resource_code=resource_code,
        resource_name=resource.resource_name.strip(),
        department_id=dept.id,
        home_depot=(
            resource.home_depot.strip()
            if resource.home_depot
            else None
        ),
        resource_type=resource.resource_type,
        availability_status=resource.availability_status,
    )

    db.add(new_resource)
    db.commit()
    db.refresh(new_resource)

    return {
        "message": "Resource added successfully.",
        "resource": {
            "id": new_resource.id,
            "resource_code": new_resource.resource_code,
            "resource_name": new_resource.resource_name,
            "department": dept.code,
            "home_depot": new_resource.home_depot,
            "resource_type": new_resource.resource_type,
            "availability_status": new_resource.availability_status,
        },
    }


@app.get(
    "/resources",
    tags=["Resources"],
    summary="List resources, optionally filtered by department",
)
def get_resources(
    department: Optional[str] = Query(
        None,
        description="Filter by department code (ENG/SNT/TD)",
    ),
    db: Session = Depends(get_db),
):
    query = db.query(models.ResourceModel)

    if department:
        department_code = department.strip().upper()

        dept = (
            db.query(models.DepartmentModel)
            .filter(
                models.DepartmentModel.code == department_code
            )
            .first()
        )

        if not dept:
            raise HTTPException(
                status_code=404,
                detail=f"Department '{department_code}' not found.",
            )

        query = query.filter(
            models.ResourceModel.department_id == dept.id
        )

    resources = (
        query
        .order_by(models.ResourceModel.resource_code)
        .all()
    )

    return {
        "count": len(resources),
        "resources": [
            {
                "id": r.id,
                "resource_code": r.resource_code,
                "resource_name": r.resource_name,
                "department": r.department.code,
                "home_depot": r.home_depot,
                "resource_type": r.resource_type,
                "availability_status": r.availability_status,
            }
            for r in resources
        ],
    }


# ===========================================================================
# Availability Windows
# ===========================================================================

@app.post(
    "/availability-windows",
    tags=["Availability Windows"],
    summary="Add a COA-derived corridor availability window",
    status_code=201,
)
def add_availability_window(
    window: schemas.AvailabilityWindowCreate,
    db: Session = Depends(get_db),
):
    block_id = window.corridor_block_id.strip().upper()

    corridor = (
        db.query(models.CorridorModel)
        .filter(
            models.CorridorModel.block_id == block_id
        )
        .first()
    )

    if not corridor:
        raise HTTPException(
            status_code=404,
            detail=f"Corridor '{block_id}' not found.",
        )

    new_window = models.AvailabilityWindowModel(
        corridor_id=corridor.id,
        date=window.date,
        start_time=window.start_time,
        end_time=window.end_time,
        is_goods_forecast_clear=window.is_goods_forecast_clear,
    )

    db.add(new_window)
    db.commit()
    db.refresh(new_window)

    return {
        "message": "Availability window added successfully.",
        "window": {
            "id": new_window.id,
            "corridor_block_id": corridor.block_id,
            "date": str(new_window.date),
            "start_time": new_window.start_time,
            "end_time": new_window.end_time,
            "is_goods_forecast_clear": new_window.is_goods_forecast_clear,
        },
    }


@app.get(
    "/availability-windows",
    tags=["Availability Windows"],
    summary="List availability windows with optional filters",
)
def get_availability_windows(
    corridor_block_id: Optional[str] = Query(
        None,
        description="Filter by corridor block ID",
    ),
    date_from: Optional[date] = Query(
        None,
        description="Filter windows from this date (YYYY-MM-DD)",
    ),
    date_to: Optional[date] = Query(
        None,
        description="Filter windows up to this date (YYYY-MM-DD)",
    ),
    db: Session = Depends(get_db),
):
    query = db.query(models.AvailabilityWindowModel)

    if corridor_block_id:
        block_id = corridor_block_id.strip().upper()

        corridor = (
            db.query(models.CorridorModel)
            .filter(
                models.CorridorModel.block_id == block_id
            )
            .first()
        )

        if not corridor:
            raise HTTPException(
                status_code=404,
                detail=f"Corridor '{block_id}' not found.",
            )

        query = query.filter(
            models.AvailabilityWindowModel.corridor_id
            == corridor.id
        )

    if date_from:
        query = query.filter(
            models.AvailabilityWindowModel.date >= date_from
        )

    if date_to:
        query = query.filter(
            models.AvailabilityWindowModel.date <= date_to
        )

    windows = (
        query
        .order_by(
            models.AvailabilityWindowModel.date,
            models.AvailabilityWindowModel.start_time,
        )
        .all()
    )

    return {
        "count": len(windows),
        "windows": [
            {
                "id": w.id,
                "corridor_block_id": w.corridor.block_id,
                "date": str(w.date),
                "start_time": w.start_time,
                "end_time": w.end_time,
                "is_goods_forecast_clear": w.is_goods_forecast_clear,
            }
            for w in windows
        ],
    }


# ===========================================================================
# Trains
# ===========================================================================

@app.post(
    "/trains",
    tags=["Trains"],
    summary="Add a train (passenger or goods)",
    status_code=201,
)
def add_train(
    train: schemas.TrainCreate,
    db: Session = Depends(get_db),
):
    train_id = train.train_id.strip()

    existing = (
        db.query(models.TrainModel)
        .filter(models.TrainModel.train_id == train_id)
        .first()
    )

    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Train '{train_id}' already exists.",
        )

    new_train = models.TrainModel(
        train_id=train_id,
        train_name=train.train_name.strip(),
        priority=train.priority,
    )

    db.add(new_train)
    db.commit()
    db.refresh(new_train)

    return {
        "message": "Train added successfully.",
        "train": {
            "id": new_train.id,
            "train_id": new_train.train_id,
            "train_name": new_train.train_name,
            "priority": new_train.priority,
        },
    }


@app.get(
    "/trains",
    tags=["Trains"],
    summary="List all trains",
)
def get_trains(
    db: Session = Depends(get_db),
):
    trains = (
        db.query(models.TrainModel)
        .order_by(models.TrainModel.train_id)
        .all()
    )

    return {
        "count": len(trains),
        "trains": [
            {
                "id": t.id,
                "train_id": t.train_id,
                "train_name": t.train_name,
                "priority": t.priority,
            }
            for t in trains
        ],
    }


# ===========================================================================
# Train Occupancy
# ===========================================================================

@app.post(
    "/train-occupancy",
    tags=["Train Occupancy"],
    summary="Record a train occupying a corridor",
    status_code=201,
)
def add_train_occupancy(
    occupancy: schemas.TrainOccupancyCreate,
    db: Session = Depends(get_db),
):
    train_id = occupancy.train_id.strip()

    train = (
        db.query(models.TrainModel)
        .filter(models.TrainModel.train_id == train_id)
        .first()
    )

    if not train:
        raise HTTPException(
            status_code=404,
            detail=f"Train '{train_id}' not found. Add it first.",
        )

    block_id = occupancy.corridor_block_id.strip().upper()

    corridor = (
        db.query(models.CorridorModel)
        .filter(
            models.CorridorModel.block_id == block_id
        )
        .first()
    )

    if not corridor:
        raise HTTPException(
            status_code=404,
            detail=f"Corridor '{block_id}' not found.",
        )

    new_occ = models.TrainOccupancyModel(
        train_id=train.id,
        corridor_id=corridor.id,
        date=occupancy.date,
        entry_time=occupancy.entry_time,
        exit_time=occupancy.exit_time,
        source=occupancy.source,
    )

    db.add(new_occ)
    db.commit()
    db.refresh(new_occ)

    return {
        "message": "Train occupancy recorded successfully.",
        "occupancy": {
            "id": new_occ.id,
            "train_id": train.train_id,
            "corridor_block_id": corridor.block_id,
            "date": str(new_occ.date),
            "entry_time": new_occ.entry_time,
            "exit_time": new_occ.exit_time,
            "source": new_occ.source,
        },
    }


@app.get(
    "/train-occupancy",
    tags=["Train Occupancy"],
    summary="List train occupancy records with optional filters",
)
def get_train_occupancy(
    corridor_block_id: Optional[str] = Query(None),
    date_filter: Optional[date] = Query(
        None,
        alias="date",
        description="Filter by date (YYYY-MM-DD)",
    ),
    source: Optional[str] = Query(
        None,
        description="timetable | goods_forecast",
    ),
    db: Session = Depends(get_db),
):
    query = db.query(models.TrainOccupancyModel)

    if corridor_block_id:
        block_id = corridor_block_id.strip().upper()

        corridor = (
            db.query(models.CorridorModel)
            .filter(
                models.CorridorModel.block_id == block_id
            )
            .first()
        )

        if not corridor:
            raise HTTPException(
                status_code=404,
                detail=f"Corridor '{block_id}' not found.",
            )

        query = query.filter(
            models.TrainOccupancyModel.corridor_id
            == corridor.id
        )

    if date_filter:
        query = query.filter(
            models.TrainOccupancyModel.date == date_filter
        )

    if source:
        source = source.strip().lower()

        if source not in (
            "timetable",
            "goods_forecast",
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "source must be "
                    "'timetable' or 'goods_forecast'"
                ),
            )

        query = query.filter(
            models.TrainOccupancyModel.source == source
        )

    occupancies = (
        query
        .order_by(
            models.TrainOccupancyModel.date,
            models.TrainOccupancyModel.entry_time,
        )
        .all()
    )

    return {
        "count": len(occupancies),
        "occupancy": [
            {
                "id": o.id,
                "train_id": o.train.train_id,
                "train_name": o.train.train_name,
                "corridor_block_id": o.corridor.block_id,
                "date": str(o.date),
                "entry_time": o.entry_time,
                "exit_time": o.exit_time,
                "source": o.source,
            }
            for o in occupancies
        ],
    }


# ===========================================================================
# Maintenance Tasks
# ===========================================================================

@app.post(
    "/maintenance-tasks",
    tags=["Maintenance Tasks"],
    summary="Add a maintenance task",
    status_code=201,
)
def add_maintenance_task(
    task: schemas.MaintenanceTaskCreate,
    db: Session = Depends(get_db),
):
    task_ref = task.task_ref.strip()

    existing = (
        db.query(models.MaintenanceTaskModel)
        .filter(
            models.MaintenanceTaskModel.task_ref
            == task_ref
        )
        .first()
    )

    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Task '{task_ref}' already exists.",
        )

    department_code = task.department_code.strip().upper()

    dept = (
        db.query(models.DepartmentModel)
        .filter(
            models.DepartmentModel.code
            == department_code
        )
        .first()
    )

    if not dept:
        raise HTTPException(
            status_code=404,
            detail=f"Department '{department_code}' not found.",
        )

    block_id = task.corridor_block_id.strip().upper()

    corridor = (
        db.query(models.CorridorModel)
        .filter(
            models.CorridorModel.block_id == block_id
        )
        .first()
    )

    if not corridor:
        raise HTTPException(
            status_code=404,
            detail=f"Corridor '{block_id}' not found.",
        )

    resource_id: Optional[int] = None

    if task.required_resource_code:
        resource_code = (
            task.required_resource_code
            .strip()
            .upper()
        )

        resource = (
            db.query(models.ResourceModel)
            .filter(
                models.ResourceModel.resource_code
                == resource_code
            )
            .first()
        )

        if not resource:
            raise HTTPException(
                status_code=404,
                detail=f"Resource '{resource_code}' not found.",
            )

        if resource.department_id != dept.id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Resource '{resource_code}' belongs "
                    f"to department "
                    f"'{resource.department.code}', "
                    f"but task department is "
                    f"'{department_code}'."
                ),
            )

        resource_id = resource.id

    new_task = models.MaintenanceTaskModel(
        task_ref=task_ref,
        department_id=dept.id,
        corridor_id=corridor.id,
        required_resource_id=resource_id,
        description=task.description.strip(),
        defect_type=task.defect_type.strip().upper(),
        criticality=task.criticality,
        reported_date=task.reported_date,
        due_date=task.due_date,
        estimated_duration_minutes=(
            task.estimated_duration_minutes
        ),
        asset_impact_score=task.asset_impact_score,
        status="pending",
    )

    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    return {
        "message": "Maintenance task added successfully.",
        "task_ref": new_task.task_ref,
        "id": new_task.id,
    }


@app.get(
    "/maintenance-tasks",
    tags=["Maintenance Tasks"],
    summary="List maintenance tasks with optional filters",
)
def get_maintenance_tasks(
    status: Optional[str] = Query(
        None,
        description=(
            "pending|scheduled|completed|"
            "deferred|cancelled"
        ),
    ),
    department: Optional[str] = Query(
        None,
        description="Filter by department code",
    ),
    corridor_block_id: Optional[str] = Query(
        None,
        description="Filter by corridor block ID",
    ),
    db: Session = Depends(get_db),
):
    query = db.query(models.MaintenanceTaskModel)

    if status:
        status = status.strip().lower()

        valid_statuses = {
            "pending",
            "scheduled",
            "completed",
            "deferred",
            "cancelled",
        }

        if status not in valid_statuses:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid status '{status}'. "
                    f"Valid: {sorted(valid_statuses)}"
                ),
            )

        query = query.filter(
            models.MaintenanceTaskModel.status
            == status
        )

    if department:
        department_code = department.strip().upper()

        dept = (
            db.query(models.DepartmentModel)
            .filter(
                models.DepartmentModel.code
                == department_code
            )
            .first()
        )

        if not dept:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Department "
                    f"'{department_code}' not found."
                ),
            )

        query = query.filter(
            models.MaintenanceTaskModel.department_id
            == dept.id
        )

    if corridor_block_id:
        block_id = corridor_block_id.strip().upper()

        corridor = (
            db.query(models.CorridorModel)
            .filter(
                models.CorridorModel.block_id
                == block_id
            )
            .first()
        )

        if not corridor:
            raise HTTPException(
                status_code=404,
                detail=f"Corridor '{block_id}' not found.",
            )

        query = query.filter(
            models.MaintenanceTaskModel.corridor_id
            == corridor.id
        )

    tasks = (
        query
        .order_by(
            models.MaintenanceTaskModel.due_date,
            models.MaintenanceTaskModel.id,
        
        )
        .all()
    )

    return {
        "count": len(tasks),
        "tasks": [
            {
                "id": t.id,
                "task_ref": t.task_ref,
                "department": t.department.code,
                "corridor_block_id": t.corridor.block_id,
                "required_resource": (
                    t.required_resource.resource_code
                    if t.required_resource
                    else None
                ),
                "description": t.description,
                "defect_type": t.defect_type,
                "criticality": t.criticality,
                "reported_date": str(t.reported_date),
                "due_date": str(t.due_date),
                "estimated_duration_minutes": (
                    t.estimated_duration_minutes
                ),
                "asset_impact_score": (
                    t.asset_impact_score
                ),
                "status": t.status,
                "created_at": str(t.created_at),
                "updated_at": str(t.updated_at),
            }
            for t in tasks
        ],
    }


# ===========================================================================
# Prioritized Maintenance Tasks
# ===========================================================================

@app.get(
    "/maintenance-tasks/prioritized",
    tags=["Maintenance Tasks"],
    summary=(
        "List pending tasks sorted by "
        "computed priority score"
    ),
)
def get_prioritized_tasks(
    db: Session = Depends(get_db),
):
    today = date.today()

    tasks = (
        db.query(models.MaintenanceTaskModel)
        .filter(
            models.MaintenanceTaskModel.status
            == "pending"
        )
        .all()
    )

    scored = sorted(
        tasks,
        key=lambda t: priority_service.compute_priority_score(
            criticality=t.criticality,
            due_date=t.due_date,
            asset_impact_score=t.asset_impact_score,
            as_of=today,
        ),
        reverse=True,
    )

    return {
        "as_of_date": str(today),
        "count": len(scored),
        "priority_formula": (
            "score = (6 - criticality) * 10 "
            "[criticality: 1=highest risk]"
            " + overdue_days * 2"
            " + urgency_bonus "
            "(approaching due date)"
            " + asset_impact_score * 0.5"
        ),
        "prioritized_tasks": [
            {
                "rank": rank + 1,
                "task_ref": t.task_ref,
                "department": t.department.code,
                "corridor_block_id": t.corridor.block_id,
                "defect_type": t.defect_type,
                "criticality": t.criticality,
                "due_date": str(t.due_date),
                "estimated_duration_minutes": (
                    t.estimated_duration_minutes
                ),
                "asset_impact_score": (
                    t.asset_impact_score
                ),
                "overdue_days": (
                    priority_service.compute_overdue_days(
                        t.due_date,
                        today,
                    )
                ),
                "priority_score": round(
                    priority_service.compute_priority_score(
                        criticality=t.criticality,
                        due_date=t.due_date,
                        asset_impact_score=(
                            t.asset_impact_score
                        ),
                        as_of=today,
                    ),
                    2,
                ),
                "status": t.status,
            }
            for rank, t in enumerate(scored)
        ],
    }


# ===========================================================================
# Block Plan Generation
# ===========================================================================

@app.post(
    "/generate-block-plan",
    tags=["Block Plan"],
    summary=(
        "Generate optimized maintenance "
        "block plan using CP-SAT"
    ),
    description=(
        "Runs Google OR-Tools CP-SAT to assign "
        "pending maintenance tasks to available "
        "corridor windows while respecting train "
        "occupancy, resources and safety constraints."
    ),
)
def generate_block_plan(
    horizon: str = Query(
        ...,
        pattern="^(weekly|monthly)$",
        description=(
            "Planning horizon: "
            "'weekly' (7 days) or "
            "'monthly' (30 days)"
        ),
    ),
    regenerate: bool = Query(
        False,
        description=(
            "Clear existing plans for this "
            "horizon before regenerating."
        ),
    ),
    body: schemas.GenerateBlockPlanRequest = Body(
        default=schemas.GenerateBlockPlanRequest(),
        description=(
            "Optional safety-incompatible "
            "defect-type pairs."
        ),
    ),
    db: Session = Depends(get_db),
):
    return scheduling_service.generate_block_plan(
        horizon=horizon,
        db=db,
        incompatible_pairs_raw=body.incompatible_pairs,
        regenerate=regenerate,
    )


# ===========================================================================
# Block Plan Retrieval
# ===========================================================================

@app.get(
    "/block-plan",
    tags=["Block Plan"],
    summary="Retrieve the generated block plan",
)
def get_block_plan(
    horizon: Optional[str] = Query(
        None,
        description="weekly | monthly",
    ),
    corridor_block_id: Optional[str] = Query(
        None,
        description="Filter by corridor block ID",
    ),
    date_from: Optional[date] = Query(
        None,
        description="Filter from date",
    ),
    date_to: Optional[date] = Query(
        None,
        description="Filter to date",
    ),
    db: Session = Depends(get_db),
):
    query = db.query(models.BlockPlanModel)

    if horizon:
        horizon = horizon.strip().lower()

        if horizon not in ("weekly", "monthly"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "horizon must be "
                    "'weekly' or 'monthly'"
                ),
            )

        query = query.filter(
            models.BlockPlanModel.horizon
            == horizon
        )

    if corridor_block_id:
        block_id = corridor_block_id.strip().upper()

        corridor = (
            db.query(models.CorridorModel)
            .filter(
                models.CorridorModel.block_id
                == block_id
            )
            .first()
        )

        if not corridor:
            raise HTTPException(
                status_code=404,
                detail=f"Corridor '{block_id}' not found.",
            )

        query = query.filter(
            models.BlockPlanModel.corridor_id
            == corridor.id
        )

    if date_from:
        query = query.filter(
            models.BlockPlanModel.scheduled_date
            >= date_from
        )

    if date_to:
        query = query.filter(
            models.BlockPlanModel.scheduled_date
            <= date_to
        )

    plans = (
        query
        .order_by(
            models.BlockPlanModel.scheduled_date,
            models.BlockPlanModel.entry_time,
        )
        .all()
    )

    return {
        "count": len(plans),
        "block_plan": [
            {
                "id": p.id,
                "task_ref": p.task.task_ref,
                "department": p.task.department.code,
                "corridor_block_id": p.corridor.block_id,
                "defect_type": p.task.defect_type,
                "block_group_id": p.block_group_id,
                "availability_window_id": (
                    p.availability_window_id
                ),
                "scheduled_date": str(
                    p.scheduled_date
                ),
                "entry_time": p.entry_time,
                "exit_time": p.exit_time,
                "priority_score": p.priority_score,
                "horizon": p.horizon,
                "resource": (
                    p.resource.resource_code
                    if p.resource
                    else None
                ),
                "created_at": str(p.created_at),
            }
            for p in plans
        ],
    }


@app.post(
    "/block-plan/reset",
    tags=["Block Plan"],
    summary=(
        "Reset ALL block plans and return "
        "tasks to pending status"
    ),
)
def reset_block_plan(
    db: Session = Depends(get_db),
):
    return scheduling_service.reset_all_plans(db)


# ===========================================================================
# Data Integration
# ===========================================================================

@app.post(
    "/data-integration/import-tms",
    tags=["Data Integration"],
    summary=(
        "Import Engineering tasks "
        "from mock TMS payload"
    ),
    status_code=201,
)
def import_tms(
    payloads: list[schemas.TmsTaskPayload],
    db: Session = Depends(get_db),
):
    raw = [p.model_dump() for p in payloads]

    return data_integration_service.import_tms_tasks(
        raw,
        db,
    )


@app.post(
    "/data-integration/import-smms",
    tags=["Data Integration"],
    summary=(
        "Import Signal & Telecom tasks "
        "from mock SMMS payload"
    ),
    status_code=201,
)
def import_smms(
    payloads: list[schemas.SmmsTaskPayload],
    db: Session = Depends(get_db),
):
    raw = [p.model_dump() for p in payloads]

    return data_integration_service.import_smms_tasks(
        raw,
        db,
    )


@app.post(
    "/data-integration/import-tdms",
    tags=["Data Integration"],
    summary=(
        "Import Traction Distribution tasks "
        "from mock TDMS payload"
    ),
    status_code=201,
)
def import_tdms(
    payloads: list[schemas.TdmsTaskPayload],
    db: Session = Depends(get_db),
):
    raw = [p.model_dump() for p in payloads]

    return data_integration_service.import_tdms_tasks(
        raw,
        db,
    )


@app.post(
    "/data-integration/import-coa-windows",
    tags=["Data Integration"],
    summary=(
        "Import corridor availability "
        "windows from mock COA payload"
    ),
    status_code=201,
)
def import_coa_windows(
    payloads: list[schemas.CoaWindowPayload],
    db: Session = Depends(get_db),
):
    raw = [p.model_dump() for p in payloads]

    return data_integration_service.import_coa_windows(
        raw,
        db,
    )


@app.post(
    "/data-integration/import-coa-occupancy",
    tags=["Data Integration"],
    summary=(
        "Import train occupancy / goods "
        "forecast from mock COA payload"
    ),
    status_code=201,
)
def import_coa_occupancy(
    payloads: list[schemas.CoaOccupancyPayload],
    db: Session = Depends(get_db),
):
    raw = [p.model_dump() for p in payloads]

    return data_integration_service.import_coa_occupancy(
        raw,
        db,
    )