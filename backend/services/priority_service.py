"""
services/priority_service.py — Priority scoring for maintenance tasks.

Stage 2:
  - Validates priority inputs
  - Provides transparent and deterministic scoring
  - Separates each scoring component
  - Supports detailed score breakdowns
  - Keeps scoring logic independent from FastAPI/database code

Scoring formula:

    priority_score = criticality_component
                   + overdue_component
                   + urgency_component
                   + impact_component

Criticality convention:
    1 = highest safety risk
    5 = lowest safety risk

Therefore:

    criticality_component = (6 - criticality) * CRITICALITY_WEIGHT

Higher score = higher scheduling priority.
"""

from __future__ import annotations

from datetime import date


# ===========================================================================
# Tunable scoring configuration
# ===========================================================================

# Safety criticality is the dominant factor.
CRITICALITY_WEIGHT: float = 10.0

# Points added for every day a task is overdue.
OVERDUE_WEIGHT: float = 2.0

# Number of days before the deadline during which urgency is considered.
URGENCY_THRESHOLD_DAYS: int = 7

# Points added for every day closer to the deadline
# while inside the urgency threshold.
URGENCY_WEIGHT: float = 3.0

# Contribution of the 0–100 asset impact score.
IMPACT_WEIGHT: float = 0.5


# ===========================================================================
# Input validation
# ===========================================================================

def _validate_inputs(
    criticality: int,
    asset_impact_score: float,
    due_date: date,
    as_of: date,
) -> None:
    """
    Validate inputs used by the priority calculation.

    Raises:
        ValueError: if a domain value is invalid.
    """

    if not isinstance(criticality, int):
        raise ValueError("criticality must be an integer.")

    if not 1 <= criticality <= 5:
        raise ValueError(
            "criticality must be between 1 and 5."
        )

    if not isinstance(asset_impact_score, (int, float)):
        raise ValueError(
            "asset_impact_score must be a number."
        )

    if not 0 <= asset_impact_score <= 100:
        raise ValueError(
            "asset_impact_score must be between 0 and 100."
        )

    if not isinstance(due_date, date):
        raise ValueError(
            "due_date must be a datetime.date."
        )

    if not isinstance(as_of, date):
        raise ValueError(
            "as_of must be a datetime.date."
        )


# ===========================================================================
# Individual scoring components
# ===========================================================================

def compute_criticality_component(
    criticality: int,
) -> float:
    """
    Calculate the safety-criticality component.

    Criticality:
        1 -> 50 points
        2 -> 40 points
        3 -> 30 points
        4 -> 20 points
        5 -> 10 points
    """

    if not isinstance(criticality, int):
        raise ValueError(
            "criticality must be an integer."
        )

    if not 1 <= criticality <= 5:
        raise ValueError(
            "criticality must be between 1 and 5."
        )

    return (6 - criticality) * CRITICALITY_WEIGHT


def compute_overdue_days(
    due_date: date,
    as_of: date,
) -> int:
    """
    Return the number of days a task is overdue.

    Returns 0 when the task is not overdue.
    """

    if not isinstance(due_date, date):
        raise ValueError(
            "due_date must be a datetime.date."
        )

    if not isinstance(as_of, date):
        raise ValueError(
            "as_of must be a datetime.date."
        )

    return max(
        0,
        (as_of - due_date).days,
    )


def compute_overdue_component(
    due_date: date,
    as_of: date,
) -> float:
    """
    Calculate the overdue component.

    Example:

        5 days overdue
        -> 5 * OVERDUE_WEIGHT
        -> 10 points
    """

    overdue_days = compute_overdue_days(
        due_date,
        as_of,
    )

    return overdue_days * OVERDUE_WEIGHT


def compute_urgency_component(
    due_date: date,
    as_of: date,
) -> float:
    """
    Calculate the approaching-deadline urgency bonus.

    A task receives an urgency bonus only when:

        0 <= days_until_due <= URGENCY_THRESHOLD_DAYS

    Example with threshold = 7:

        7 days remaining -> 0 points
        6 days remaining -> 3 points
        3 days remaining -> 12 points
        1 day remaining  -> 18 points
        due today        -> 21 points

    Overdue tasks receive no urgency bonus because overdue priority
    is handled separately by the overdue component.
    """

    days_until_due = (
        due_date - as_of
    ).days

    if 0 <= days_until_due <= URGENCY_THRESHOLD_DAYS:
        return (
            URGENCY_THRESHOLD_DAYS
            - days_until_due
        ) * URGENCY_WEIGHT

    return 0.0


def compute_impact_component(
    asset_impact_score: float,
) -> float:
    """
    Calculate the asset-impact component.

    asset_impact_score:
        0   -> 0 points
        50  -> 25 points
        100 -> 50 points
    """

    if not isinstance(
        asset_impact_score,
        (int, float),
    ):
        raise ValueError(
            "asset_impact_score must be a number."
        )

    if not 0 <= asset_impact_score <= 100:
        raise ValueError(
            "asset_impact_score must be between 0 and 100."
        )

    return (
        asset_impact_score
        * IMPACT_WEIGHT
    )


# ===========================================================================
# Complete priority score
# ===========================================================================

def compute_priority_score(
    criticality: int,
    due_date: date,
    asset_impact_score: float,
    as_of: date,
) -> float:
    """
    Compute the final transparent priority score.

    Higher score = higher scheduling priority.

    Formula:

        score =
            criticality_component
            + overdue_component
            + urgency_component
            + impact_component
    """

    _validate_inputs(
        criticality=criticality,
        asset_impact_score=asset_impact_score,
        due_date=due_date,
        as_of=as_of,
    )

    criticality_component = (
        compute_criticality_component(
            criticality
        )
    )

    overdue_component = (
        compute_overdue_component(
            due_date,
            as_of,
        )
    )

    urgency_component = (
        compute_urgency_component(
            due_date,
            as_of,
        )
    )

    impact_component = (
        compute_impact_component(
            asset_impact_score
        )
    )

    return (
        criticality_component
        + overdue_component
        + urgency_component
        + impact_component
    )


# ===========================================================================
# Detailed scoring breakdown
# ===========================================================================

def get_priority_breakdown(
    criticality: int,
    due_date: date,
    asset_impact_score: float,
    as_of: date,
) -> dict:
    """
    Return a detailed explanation of the priority score.

    This is useful for:
      - dashboard display
      - API responses
      - debugging
      - SIH demonstration
      - explaining why one task ranks above another
    """

    _validate_inputs(
        criticality=criticality,
        asset_impact_score=asset_impact_score,
        due_date=due_date,
        as_of=as_of,
    )

    criticality_component = (
        compute_criticality_component(
            criticality
        )
    )

    overdue_days = compute_overdue_days(
        due_date,
        as_of,
    )

    overdue_component = (
        compute_overdue_component(
            due_date,
            as_of,
        )
    )

    urgency_component = (
        compute_urgency_component(
            due_date,
            as_of,
        )
    )

    impact_component = (
        compute_impact_component(
            asset_impact_score
        )
    )

    total_score = (
        criticality_component
        + overdue_component
        + urgency_component
        + impact_component
    )

    return {
        "priority_score": round(
            total_score,
            2,
        ),
        "criticality_component": round(
            criticality_component,
            2,
        ),
        "overdue_days": overdue_days,
        "overdue_component": round(
            overdue_component,
            2,
        ),
        "urgency_component": round(
            urgency_component,
            2,
        ),
        "impact_component": round(
            impact_component,
            2,
        ),
    }


# ===========================================================================
# Dictionary helper
# ===========================================================================

def score_task_dict(
    task_dict: dict,
    as_of: date,
) -> float:
    """
    Compute priority score from a plain dictionary.

    Used by the optimizer and other service-layer components.

    Required dictionary fields:

        criticality
        due_date
        asset_impact_score
    """

    return compute_priority_score(
        criticality=task_dict["criticality"],
        due_date=task_dict["due_date"],
        asset_impact_score=task_dict[
            "asset_impact_score"
        ],
        as_of=as_of,
    )