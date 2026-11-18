"""Namespace-scoped rider retirement and review-event helpers."""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from webapp.models.ns.activity_monitor import (
    ActivityMonitorRider,
    ActivityMonitorReview,
    ActivityMonitorReviewEvent,
    ActivityMonitorRun,
    ActivityMonitorTask,
    ActivityMonitorTaskEvent,
)


TERMINAL_TASK_STATES = {"completed", "needs_review", "failed", "cancelled"}


def retire_activity_monitor_rider(engine, rider_id, *, actor_user_id=None):
    """Mark a namespace rider retired and cancel tasks that have not started."""
    now = datetime.utcnow()
    session = Session(engine)
    try:
        with session.begin():
            rider = session.scalar(
                select(ActivityMonitorRider)
                .where(ActivityMonitorRider.id == int(rider_id))
                .with_for_update()
            )
            if rider is None:
                raise ValueError("Rider was not found in the selected namespace.")

            review = session.scalar(
                select(ActivityMonitorReview)
                .where(
                    ActivityMonitorReview.rider_id == rider.id,
                    ActivityMonitorReview.identity_generation == rider.identity_generation,
                    ActivityMonitorReview.review_kind == "retirement",
                )
                .with_for_update()
            )
            old_status = review.review_status if review else None
            if review is None:
                review = ActivityMonitorReview(
                    rider_id=rider.id,
                    identity_generation=rider.identity_generation,
                    review_kind="retirement",
                    review_status="retired",
                    notes="Retired by an administrator.",
                    supporting_links=[],
                    last_reviewed_at=now,
                    reviewed_by_user_id=actor_user_id,
                    updated_at=now,
                )
                session.add(review)
            else:
                review.review_status = "retired"
                review.notes = "Retired by an administrator."
                review.last_reviewed_at = now
                review.reviewed_by_user_id = actor_user_id
                review.updated_at = now
            session.flush()
            session.add(ActivityMonitorReviewEvent(
                source_record_key=f"retirement:{rider.id}:{rider.identity_generation}:{now.isoformat()}:{actor_user_id or 0}",
                rider_id=rider.id,
                identity_generation=rider.identity_generation,
                review_kind="retirement",
                old_status=old_status,
                new_status="retired",
                notes="Rider retired by an administrator.",
                supporting_links=[],
                actor_user_id=actor_user_id,
                occurred_at=now,
            ))

            pending_tasks = session.scalars(
                select(ActivityMonitorTask)
                .where(
                    ActivityMonitorTask.rider_id == rider.id,
                    ActivityMonitorTask.identity_generation == rider.identity_generation,
                    ActivityMonitorTask.state.in_({"queued", "retry_wait"}),
                )
                .with_for_update()
            ).all()
            affected_runs = set()
            for task in pending_tasks:
                old_state = task.state
                task.state = "cancelled"
                task.next_retry_at = None
                task.updated_at = now
                affected_runs.add(task.run_id)
                session.add(ActivityMonitorTaskEvent(
                    source_record_key=f"task-event:{uuid4()}",
                    task_id=task.id,
                    old_state=old_state,
                    new_state="cancelled",
                    actor_user_id=actor_user_id,
                    occurred_at=now,
                    notes="Cancelled because the rider was retired.",
                ))

            for run_id in affected_runs:
                run = session.scalar(
                    select(ActivityMonitorRun)
                    .where(ActivityMonitorRun.id == run_id)
                    .with_for_update()
                )
                if run is None:
                    continue
                states = session.scalars(
                    select(ActivityMonitorTask.state)
                    .where(ActivityMonitorTask.run_id == run.id)
                ).all()
                run.attempted_count = sum(
                    state in {"running", "retry_wait", "completed", "needs_review", "failed"}
                    for state in states
                )
                run.successful_count = sum(state in {"completed", "needs_review"} for state in states)
                run.matched_count = states.count("completed")
                run.needs_review_count = states.count("needs_review")
                run.heartbeat_at = now
                if states and all(state in TERMINAL_TASK_STATES for state in states):
                    run.status = "cancelled" if all(state == "cancelled" for state in states) else "completed"
                    run.finished_at = now

            return {"rider_id": rider.id, "cancelled_count": len(pending_tasks)}
    finally:
        session.close()
