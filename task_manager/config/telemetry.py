"""
OpenTelemetry setup (metrics + traces + logs), shared by the web (Django) and worker
(Celery) processes.

Data flow:
  metrics: this process --OTLP/HTTP--> otel-collector --scrape--> Prometheus --> Grafana
  traces:  this process --OTLP/HTTP--> otel-collector --OTLP/gRPC--> Tempo  --> Grafana
  logs:    this process --OTLP/HTTP--> otel-collector --OTLP/HTTP--> Loki   --> Grafana

Where each piece gets called:
  * web:    config/wsgi.py and config/asgi.py. `runserver` imports config.wsgi only in
            the process that serves requests, so the autoreloader parent process and
            commands like `migrate` never start an exporter.
  * worker: config/celery.py, from the `worker_process_init` signal. Celery's prefork
            pool forks child processes, and an exporter's background thread does not
            survive a fork, so every child has to set up its own.

All configuration comes from standard OTEL_* environment variables (see .env and
docker-compose.yml):
  OTEL_SERVICE_NAME              -> Prometheus `job` label / service name in Tempo
  OTEL_EXPORTER_OTLP_ENDPOINT    -> where the collector listens (http://otel-collector:4318)
  OTEL_METRIC_EXPORT_INTERVAL    -> how often to push metrics, in ms
"""
import logging
import os
import socket

logger = logging.getLogger(__name__)

_meter_provider = None
_tracer_provider = None
_logger_provider = None

# Never export these loggers' records: if exporting fails, the exporter logs an
# error, which would be exported, fail again, and loop.
_NOT_EXPORTED_LOGGERS = ("opentelemetry", "urllib3")


def _exportable(record):
    return not record.name.startswith(_NOT_EXPORTED_LOGGERS)


def setup_telemetry():
    """Create the global MeterProvider, TracerProvider and LoggerProvider once per process.

    Returns True if telemetry is on.
    """
    global _meter_provider, _tracer_provider, _logger_provider
    if _meter_provider is not None:
        return True
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set; OpenTelemetry disabled")
        return False

    from opentelemetry import metrics, trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # Resource = "who is sending". Shared by metrics and traces, so the same
    # service.name shows up in Prometheus (as `job`) and in Tempo.
    # Resource.create() also reads OTEL_SERVICE_NAME.
    # service.instance.id must be unique per *process*: several Celery children export
    # the same metric names, and without a distinct id the collector would treat them
    # as one series and overwrite their counters with each other's values.
    resource = Resource.create(
        {"service.instance.id": f"{socket.gethostname()}-{os.getpid()}"}
    )

    # --- Metrics: the reader wakes up every OTEL_METRIC_EXPORT_INTERVAL ms and
    # POSTs everything recorded so far to <endpoint>/v1/metrics.
    reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    _meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(_meter_provider)

    # --- Traces: finished spans are queued in memory and a background thread
    # POSTs them in batches to <endpoint>/v1/traces, so requests never wait on export.
    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_tracer_provider)

    # --- Logs: batched and sent in the background to <endpoint>/v1/logs.
    # The handler that feeds this provider is attached separately, see attach_log_handler().
    _logger_provider = LoggerProvider(resource=resource)
    _logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(_logger_provider)

    logger.info(
        "OpenTelemetry metrics+traces+logs enabled for %s (pid %s)",
        os.environ.get("OTEL_SERVICE_NAME", "unknown_service"),
        os.getpid(),
    )
    return True


def attach_log_handler():
    """Add the OTel log handler to the root logger (the console handler stays).

    When a record is emitted while a span is active (inside a request or task), the
    handler copies that span's trace_id and span_id onto it, so existing
    logger.info(...) calls get trace context without any changes.

    Must run AFTER the last logging (re)configuration in the process: dictConfig
    replaces the root logger's handlers. In the web process, get_wsgi_application()
    calls django.setup() again, which re-applies settings.LOGGING, so wsgi.py/asgi.py
    call this after it. Safe to call more than once.
    """
    if _logger_provider is None:
        return
    from opentelemetry.sdk._logs import LoggingHandler

    root = logging.getLogger()
    if any(isinstance(h, LoggingHandler) for h in root.handlers):
        return
    handler = LoggingHandler(level=logging.INFO, logger_provider=_logger_provider)
    handler.addFilter(_exportable)
    root.addHandler(handler)


def shutdown_telemetry():
    """Flush anything not yet exported. Celery children exit without running atexit hooks."""
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
    if _logger_provider is not None:
        _logger_provider.shutdown()
    if _meter_provider is not None:
        _meter_provider.shutdown()


def _instrument_process_cpu():
    """CPU time of THIS process (psutil), exported as process.cpu.time{type=user|system}.

    In Prometheus: process_cpu_time_seconds_total. rate() of it = CPU cores in use
    (1.0 = one full core). Only this one metric is enabled, to keep the series count small.
    Each Celery child reports its own process; the Celery main process is not covered.
    """
    from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor

    SystemMetricsInstrumentor(config={"process.cpu.time": ["user", "system"]}).instrument()


def instrument_django():
    """Web process. Must run before Django builds its middleware chain
    (i.e. before get_wsgi_application)."""
    if not setup_telemetry():
        return
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    # Adds a middleware that starts the root span of each request and records
    # http.server.request.duration. With OTEL_SEMCONV_STABILITY_OPT_IN=http the route
    # label is the URL *pattern* (e.g. "api/tasks/<pk>/"), never the real path.
    DjangoInstrumentor().instrument()
    # One child span per SQL query (query text with %s placeholders, no values).
    PsycopgInstrumentor().instrument()
    # Spans for the Django cache (GET/SET) and for the LPUSH that queues Celery tasks.
    RedisInstrumentor().instrument()
    # Sending side: puts the trace context (traceparent) into each task message's
    # headers, so the worker's span joins this request's trace.
    CeleryInstrumentor().instrument()
    _instrument_process_cpu()


def instrument_celery():
    """Worker process. Call inside each Celery child process (worker_process_init)."""
    if not setup_telemetry():
        return
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    # Nothing reconfigures logging after worker_process_init, so attach right away.
    attach_log_handler()
    # Running side: reads the traceparent header and continues the web request's trace.
    # Also records flower.task.runtime.seconds (labels: task name, worker hostname).
    CeleryInstrumentor().instrument()
    # SQL spans for the task's own queries.
    PsycopgInstrumentor().instrument()
    _instrument_process_cpu()
    # Redis is deliberately NOT instrumented here: the worker polls the broker
    # (BRPOP) constantly, outside any task, and each poll would become its own
    # one-span trace that floods Tempo.
