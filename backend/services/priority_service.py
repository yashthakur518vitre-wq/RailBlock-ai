"""
services/priority_service.py — Priority scoring for maintenance tasks.

Scoring formula (transparent, explainable):

    priority_score = criticality_component
                   + overdue_component
                   + urgency_component
                   + impact_component

Criticality convention (Indian Railways safety):
    1 = highest safety risk (e.g. rail fracture, OHE live-wire fault)
    5 = lowest priority (cosmetic / routine)
    → Higher criticality NUMBER means LOWER priority
    → Score contribution: (6 - criticality) * CRITICALITY_WEIGHT

Overdue component:
    Days past due_date * OVERDUE_WEIGHT

Urgency component:
    Tasks approaching due_date (within URGENCY_THRESHOLD_DAYS) get a bonus
    based on how close they are to their deadline.

Asset impact component:
    asset_impact_score (0–100 scale) * IMPACT_WEIGHT
"""

from __future__ import annotations

from datetime import date

# ---------------------------------------------------------------------------
# Tunable weights — adjust these to reflect Indian Railways policy priorities
# ---------------------------------------------------------------------------

CRITICALITY_WEIGHT: int = 10   # Safety criticality is the dominant factor
OVERDUE_WEIGHT: float = 2.0    # Penalty per day overdue
URGENCY_THRESHOLD_DAYS: int = 7  # Tasks due within 7 days get urgency bonus
URGENCY_WEIGHT: float = 3.0    # Points added per day inside urgency threshold
IMPACT_WEIGHT: float = 0.5     # Asset impact score contribution


def compute_priority_score(
    criticality: int,
    due_date: date,
    asset_impact_score: float,
    as_of: date,
) -> float:
    """
    Compute a transparent priority score for a maintenance task.

    Higher score = higher scheduling priority.

    Args:
        criticality:       1 (most critical) to 5 (least critical).
        due_date:          Date by which the task must be completed.
        asset_impact_score: 0–100 scale — domain-expert assessment of
                            impact on asset availability if deferred.
        as_of:             Reference date (usually today).

    Returns:
        Float priority score (higher = schedule sooner).
    """
    # Safety criticality: criticality=1 → contributes 50 points
    criticality_component = (6 - criticality) * CRITICALITY_WEIGHT

    # Overdue: positive only if past due_date
    overdue_days = max(0, (as_of - due_date).days)
    overdue_component = overdue_days * OVERDUE_WEIGHT

    # Urgency: approaching-deadline bonus (not yet overdue)
    days_until_due = (due_date - as_of).days
    if 0 <= days_until_due <= URGENCY_THRESHOLD_DAYS:
        # The closer to the deadline, the higher the urgency bonus
        urgency_component = (URGENCY_THRESHOLD_DAYS - days_until_due) * URGENCY_WEIGHT
    else:
        urgency_component = 0.0

    # Asset impact
    impact_component = asset_impact_score * IMPACT_WEIGHT

    return (
        criticality_component
        + overdue_component
        + urgency_component
        + impact_component
    )


def compute_overdue_days(due_date: date, as_of: date) -> int:
    """Return number of days a task is overdue (0 if not overdue)."""
    return max(0, (as_of - due_date).days)


def score_task_dict(task_dict: dict, as_of: date) -> float:
    """Compute priority score from a plain dict (used by the optimizer)."""
    return compute_priority_score(
        criticality=task_dict["criticality"],
        due_date=task_dict["due_date"],
        asset_impact_score=task_dict["asset_impact_score"],
        as_of=as_of,
    )
