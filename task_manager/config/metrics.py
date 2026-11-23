"""
Hand-written Prometheus metrics for Django (no django-prometheus).

Contains three things:
  1. Metric definitions  - Counter, Histogram, Gauge
  2. MetricsMiddleware   - updates the metrics on every request
  3. metrics_view        - the /metrics page Prometheus scrapes
"""
import os
import time

from django.http import HttpResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ---------------------------------------------------------------------------
# 1. Metric definitions
# ---------------------------------------------------------------------------
# Counter: only goes up. Use rate() in Grafana to get "per second".
REQUESTS_TOTAL = Counter(
    "django_http_requests_total",
    "Total HTTP requests handled by Django",
    ["method", "endpoint", "status"],
)

# Histogram: records how long things take, sorted into buckets
# (5ms, 10ms, 25ms ... 10s). Lets Grafana calculate p50 / p95 / p99.
REQUEST_LATENCY = Histogram(
    "django_http_request_duration_seconds",
    "Time spent processing a request",
    ["method", "endpoint"],
)

# Gauge: goes up and down. Shows how many requests are being processed now.
# multiprocess_mode is ignored on runserver; used only under gunicorn.
REQUESTS_IN_PROGRESS = Gauge(
    "django_http_requests_in_progress",
    "Requests currently being processed",
    ["method"],
    multiprocess_mode="livesum",
)

# Don't count Prometheus's own scrapes - they would add noise every 15s.
EXCLUDED_PATHS = {"/metrics"}


def _endpoint_label(request):
    """
    Turn a request into a SHORT, FIXED label.

    Never use request.path directly: /api/tasks/1/, /api/tasks/2/ ... would
    each become a new time series (called "high cardinality") and slowly
    eat Prometheus's memory.

    view_name gives names like "task-list" / "task-detail" for DRF router
    views, or the view's dotted path for others.
    """
    match = getattr(request, "resolver_match", None)
    if match is None:
        return "unmatched"  # 404s for unknown URLs share one label
    return match.view_name or match.route or "unknown"


# ---------------------------------------------------------------------------
# 2. Middleware
# ---------------------------------------------------------------------------
class MetricsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in EXCLUDED_PATHS:
            return self.get_response(request)

        method = request.method
        REQUESTS_IN_PROGRESS.labels(method).inc()
        start = time.perf_counter()
        status = "500"  # assume failure in case the view raises an exception
        try:
            response = self.get_response(request)
            status = str(response.status_code)
            return response
        finally:
            # resolver_match is set during URL routing, so read it afterwards
            duration = time.perf_counter() - start
            endpoint = _endpoint_label(request)
            REQUEST_LATENCY.labels(method, endpoint).observe(duration)
            REQUESTS_TOTAL.labels(method, endpoint, status).inc()
            REQUESTS_IN_PROGRESS.labels(method).dec()


# ---------------------------------------------------------------------------
# 3. /metrics view
# ---------------------------------------------------------------------------
def metrics_view(request):
    """
    Output every metric in Prometheus text format.

    runserver (single process): read this process's memory directly.
    gunicorn  (multi process):  if PROMETHEUS_MULTIPROC_DIR is set, read and
                                add up the files written by all workers.
    """
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        data = generate_latest(registry)
    else:
        data = generate_latest()  # default registry = this process

    return HttpResponse(data, content_type=CONTENT_TYPE_LATEST)