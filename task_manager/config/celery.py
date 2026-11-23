import os

from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("task_manager")
# Reads every setting that starts with CELERY_ from Django settings
app.config_from_object("django.conf:settings", namespace="CELERY")
# Finds tasks.py in every installed app
app.autodiscover_tasks()


# Prefork pool: the main worker process forks children that run the tasks.
# OpenTelemetry starts a background export thread, and threads don't survive a fork,
# so each child sets up its own telemetry right after it is created.
@worker_process_init.connect(weak=False)
def init_worker_telemetry(**kwargs):
    from config.telemetry import instrument_celery

    instrument_celery()


@worker_process_shutdown.connect(weak=False)
def flush_worker_telemetry(**kwargs):
    from config.telemetry import shutdown_telemetry

    shutdown_telemetry()
