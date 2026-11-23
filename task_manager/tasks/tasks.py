"""Celery background jobs."""
import logging
import time

from celery import shared_task

from .models import Task

logger = logging.getLogger(__name__)


@shared_task
def send_assignment_notification(task_id):
    """
    Pretend to notify the assignee (e.g. email). Runs in the Celery worker,
    so it shows up as a separate span when tracing is added later.
    """
    try:
        task = Task.objects.select_related("assigned_to", "created_by").get(id=task_id)
    except Task.DoesNotExist:
        logger.warning("Task %s no longer exists, skipping notification", task_id)
        return None

    if task.assigned_to is None:
        return None

    time.sleep(1)  # simulate slow work (sending an email)
    message = (
        f"Hi {task.assigned_to.username}, {task.created_by.username} "
        f"assigned you the task '{task.task_name}'."
    )
    logger.info("NOTIFICATION: %s", message)
    return message
