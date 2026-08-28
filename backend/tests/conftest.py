"""
tests/conftest.py — Shared fixtures for the RailBlock AI test suite.

Provides an in-memory SQLite database and pre-seeded reference data
used by the scheduling/optimizer tests.
"""

import sys
import os
import pytest
from datetime import date, timedelta

# Ensure the backend root is importable.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
import models


@pytest.fixture()
def db_session():
    """Create a fresh in-memory SQLite database for each test."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def seed_data(db_session):
    """
    Pre-populate the DB with two departments, one corridor, two resources,
    and return a helper dict with references.
    """
    dept_eng = models.DepartmentModel(code="ENG", name="Engineering")
    dept_snt = models.DepartmentModel(code="SNT", name="Signal & Telecom")
    db_session.add_all([dept_eng, dept_snt])
    db_session.flush()

    corridor = models.CorridorModel(
        block_id="BLK-001",
        block_name="Test Block",
        start_station="A",
        end_station="B",
        line_capacity_per_day=1,
        annual_gmt=120,
    )
    db_session.add(corridor)
    db_session.flush()

    res1 = models.ResourceModel(
        resource_code="TM-01",
        resource_name="Tamping Machine 01",
        department_id=dept_eng.id,
        home_depot="DEPOT-A",
        resource_type="machine",
        availability_status="available",
    )
    res2 = models.ResourceModel(
        resource_code="TM-02",
        resource_name="Tamping Machine 02",
        department_id=dept_eng.id,
        home_depot="DEPOT-A",
        resource_type="machine",
        availability_status="unavailable",
    )
    db_session.add_all([res1, res2])
    db_session.flush()

    today = date.today()

    return {
        "db": db_session,
        "dept_eng": dept_eng,
        "dept_snt": dept_snt,
        "corridor": corridor,
        "res_available": res1,
        "res_unavailable": res2,
        "today": today,
    }
