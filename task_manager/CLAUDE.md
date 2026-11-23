# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Task manager built on Django 5.1, Django REST Framework, PostgreSQL 16, Redis and Celery, all run through Docker Compose. The frontend is plain HTML templates plus vanilla JS that call the REST API with `fetch`. It has no build step and no JS framework.

## Layout note

The Django project root (where `manage.py`, `docker-compose.yml` and this file live) is `D:\Practice\task_manager\task_manager`. The parent directory `D:\Practice\task_manager` holds the local virtualenv `env/`. That venv exists only so the editor can resolve imports; the app itself runs in containers.

## Commands

Run all of these from the Django project root.

```powershell
docker compose up -d --build                                # start everything (never `down`, see Rules)
docker compose exec web python manage.py makemigrations     # after model changes
docker compose exec web python manage.py migrate            # also runs automatically when web starts
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py test               # run all tests
docker compose exec web python manage.py test tasks         # one app
docker compose exec web python manage.py test tasks.tests.SomeTestCase.test_method   # single test
docker compose logs -f worker                               # Celery output (notifications are logged here)
docker compose restart otel-collector                       # after editing observability/otel-collector/config.yaml
docker compose restart grafana                              # after editing provisioning files
docker compose restart tempo                                # after editing observability/tempo/tempo.yaml
docker compose restart loki                                 # after editing observability/loki/loki.yaml
curl -X POST http://localhost:9090/-/reload                 # after editing observability/prometheus/prometheus.yml
```

The repo has no tests yet, and no linter or formatter is configured. Source is mounted into the containers (`.:/app`), so `runserver` picks up code changes automatically. The Celery `worker` does not reload, so run `docker compose restart worker` after changing `tasks/tasks.py`.

The defaults in `config/settings.py` point to `localhost` for Postgres and Redis. Inside Compose, `.env` overrides them with the service hostnames (`db`, `redis`). To run `manage.py` on the host instead, the `db`/`redis` containers must be up. Postgres is published on host port **5433** (5432 is taken by a locally installed Postgres), so set `POSTGRES_PORT=5433` for host runs.

## Architecture

- **`config/`**: settings, the root URLconf and the Celery app. `config/__init__.py` imports the Celery app so it loads with Django. Celery reads every `CELERY_*` setting and autodiscovers `tasks.py` in each installed app. Redis databases: `/0` is the Django cache, `/1` the Celery broker, `/2` the Celery results.
- **`accounts/`**: auth only; it has no models and uses `django.contrib.auth.models.User`. DRF `TokenAuthentication` is the only auth class, and `IsAuthenticated` is the default permission, so public endpoints must set `AllowAny` explicitly. Signup and login both return `{token, user}`. Logout deletes the user's token.
  - `/api/auth/users/` (the "Assign to" dropdown) is cached in Redis under `accounts:user_list`. `accounts/signals.py` clears that key on User `post_save`/`post_delete`, and `AccountsConfig.ready()` registers the signals.
- **`tasks/`**: the `Task` model, with `created_by` (CASCADE) and `assigned_to` (nullable, SET_NULL). The model is exposed through a `ModelViewSet` at `/api/tasks/`. Access rules are enforced in three places, so check all three when you change them:
  1. `TaskViewSet.get_queryset`: users see only tasks they created or are assigned to. Anyone else gets a 404, not a 403.
  2. `permissions.IsCreatorOrAssignee`: the creator gets full CRUD; the assignee can view and edit but not delete.
  3. `TaskSerializer.validate_assigned_to`: only the creator may change `assigned_to`.
- **Background jobs**: `TaskViewSet.perform_create` and `perform_update` call `send_assignment_notification.delay(task.id)` when a task gets a new assignee. The job runs in the `worker` container; it simulates sending an email with `time.sleep` and logs the message. Pass IDs, not model instances, because the serializer is JSON-only.
- **Frontend**: `config/urls.py` serves `templates/{tasks,login,signup}.html` through `TemplateView`. `static/js/api.js` defines the `api()` fetch wrapper and the `Auth` helper. The token and user are kept in `localStorage`, and a 401 response clears them and redirects to `/login/`. `auth.js` is shared by the login and signup pages through `form.dataset.mode`. User-supplied text must go through `escapeHtml()` before it is inserted into HTML.
- Password validators are intentionally disabled (`AUTH_PASSWORD_VALIDATORS = []`); signup only enforces `min_length=4`.
- The code has comments mentioning that "tracing" will be added later. Keep the logging and the web/worker separation intact.

## Observability (Phase 1: metrics, Phase 2: traces, Phase 3: logs)

```
metrics: web, worker --OTLP/HTTP :4318--> otel-collector --(Prometheus scrapes :8889)--> prometheus :9090 --> grafana :3000
traces:  web, worker --OTLP/HTTP :4318--> otel-collector --OTLP/gRPC :4317--> tempo :3200 (internal) --> grafana
logs:    web, worker --OTLP/HTTP :4318--> otel-collector --OTLP/HTTP /otlp--> loki :3100 (internal) --> grafana
```

- `config/telemetry.py` builds the OTel `MeterProvider`, configured entirely by `OTEL_*` env vars (`.env`; `OTEL_SERVICE_NAME` is set per service in `docker-compose.yml`). Each process gets `service.instance.id = <hostname>-<pid>` so prefork children don't overwrite each other's series. That becomes the Prometheus `instance` label, and `service.name` becomes `job`.
- **Web:** `instrument_django()` is called from `config/wsgi.py`/`asgi.py`, not from `manage.py`. `runserver` imports `config.wsgi` only in the serving child process, so the autoreloader parent and commands like `migrate` don't export anything.
- **Worker:** `instrument_celery()` runs in the `worker_process_init` signal (`config/celery.py`), and `worker_process_shutdown` flushes. Never set up OTel at import time in the Celery parent, because the export thread does not survive the fork.
- `OTEL_SEMCONV_STABILITY_OPT_IN=http` makes Django metrics use `http_route` (the URL *pattern*, e.g. `api/tasks/(?P<pk>[^/.]+)/$`); unmatched 404s have no route label. Metrics: `http_server_request_duration_seconds_*`, `http_server_active_requests`, `flower_task_runtime_seconds_*` (labels `task`, `worker`).
- Prometheus uses `honor_labels: true` on the collector job so `job`/`instance` come from the app, not the scrape target.
- Grafana's Prometheus data source is provisioned from `observability/grafana/provisioning/datasources/` with fixed `uid: prometheus`. Grafana gets only `GF_SECURITY_ADMIN_*` from `.env` (via compose interpolation, not `env_file`). The admin password env var applies only when the `grafana_data` volume is first created; to change it later run `docker compose exec grafana sh -c 'grafana cli admin reset-admin-password "$GF_SECURITY_ADMIN_PASSWORD"'`.
- `config/metrics.py` (prometheus_client middleware plus a `/metrics` route) is an older hand-written approach that still runs, but nothing scrapes it. It is excluded from OTel via `OTEL_PYTHON_DJANGO_EXCLUDED_URLS`.
- **Traces:** `setup_telemetry()` also installs a `TracerProvider` + `BatchSpanProcessor`. Instrumentation by process:
  - web: Django, psycopg, Redis, and Celery (sending side)
  - worker: Celery (running side) and psycopg
  Celery must be instrumented in the **web** process too: its publish hook injects `traceparent` into the task headers, which is what makes web and worker spans one trace. Redis is intentionally **not** instrumented in the worker, because the broker polling (BRPOP) would create a flood of one-span root traces.
- SQL spans record statements with `%s` placeholders and Redis spans mask arguments (`LPUSH ? ?`), so values never reach Tempo. Keep `capture_parameters` off.
- Tempo (`observability/tempo/tempo.yaml`) runs single-binary with local storage and `block_retention: 48h`; its OTLP receiver must bind `0.0.0.0` (Tempo 2.7+ defaults to localhost). `tempo-init` chowns the `tempo_data` volume to uid 10001 before Tempo starts. The metrics generator is deliberately off.
- Grafana's Tempo data source (`uid: tempo`) has `tracesToMetrics` mapping `service.name` → Prometheus `job`. Grafana expands `$VARS` in provisioning files, so a literal `$` is written as `$$`.
- **Logs:** `setup_telemetry()` creates a `LoggerProvider`; `attach_log_handler()` adds an OTel `LoggingHandler` to the root logger, next to the console handler. Records emitted while a span is active get `trace_id`/`span_id` automatically, so existing `logger.info(...)` calls need no changes. Records from `opentelemetry.*`/`urllib3.*` are filtered out to avoid an export-error feedback loop.
  - **Gotcha:** in the web process `attach_log_handler()` must run *after* `get_wsgi_application()`/`get_asgi_application()`. Those call `django.setup()` again, which re-applies `settings.LOGGING` via dictConfig and drops any root handler added earlier. Instrumentation, by contrast, must run *before* them. The worker attaches the handler in `worker_process_init`.
  - Not exported or no trace ID (expected): Celery MainProcess lines ("Task received", "ready"), because OTel runs only in the forked children; `django.server` access lines (their own non-propagating handler); startup lines.
- Loki (`observability/loki/loki.yaml`): single-binary, tsdb/schema v13, 48h retention enforced by the compactor (`retention_enabled: true`). `limits_config.otlp_config` indexes **only** `service.name` as a label (`service_name`). Loki's defaults would also index `service.instance.id`. `trace_id`, `span_id`, instance id, code location and severity are structured metadata, filtered with `| trace_id="..."` after the stream selector, never inside `{}`.
- Grafana links: Loki `derivedFields` (`matcherType: label` on `trace_id`) gives a "View trace" button to Tempo. Tempo `tracesToLogsV2` runs `{service_name=~"task-manager-.+"} | trace_id="<trace>"`, so it shows web *and* worker lines for the trace.
- **Dashboards as code:** `observability/grafana/provisioning/dashboards/dashboards.yml` loads every JSON in `dashboards/json/` into the "Task Manager" folder (`allowUiUpdates: false`, so edit the JSON, not the UI). `task-manager-load-test.json` (uid `task-manager-load-test`) has HTTP RED panels, Celery, a Tempo traces table filtered by `$run_id`, and Loki logs. Use `http_route=~"${route:regex}"` for the route variable, because route values contain regex characters.
- `task-manager-cpu.json` (uid `task-manager-cpu`) is a separate CPU dashboard for web and Celery; keep CPU panels out of the load-test dashboard. It uses `process_cpu_time_seconds_total{type=user|system}` from `_instrument_process_cpu()` in `config/telemetry.py` (SystemMetricsInstrumentor restricted to `process.cpu.time`; package `opentelemetry-instrumentation-system-metrics`). `rate()` of it = cores in use, shown as percentunit (100% = 1 core, as in `docker stats`). The Celery main process is not measured; only the forked children are.
- Observability services have `restart: unless-stopped`. Tempo was OOM-killed at its 512 MiB limit by load-test verification doing 10 parallel trace-by-id lookups; the verifier now waits 8 s and uses 4 workers (peak ~94 MiB for 300 traces). Keep trace lookups gentle.
- `flower_task_runtime_seconds` uses the SDK's default histogram buckets (0, 5, 10 … 10000), which are too coarse for ~1 s tasks, so use sum/count averages, not `histogram_quantile`. Fixing it needs an SDK `View` with explicit buckets in `config/telemetry.py`.
- **Load test:** `docker compose run --rm --no-deps -T -e OTEL_SERVICE_NAME=load-generator web python -m loadtest.run_load_test --rps 10 --duration 10` (or the `telemetry-load-tester` agent). It verifies the Prometheus deltas, the Tempo traces and the Loki lines, and writes `loadtest/results/<run_id>.json`. Grafana's `/api/ds/query` can't run TraceQL *search* ("backend TraceQL search queries are not supported"), so test it via `/api/datasources/proxy/uid/tempo/api/search`.
- To check traces from the CLI (Tempo isn't published), go through Grafana's proxy with admin basic auth: `http://localhost:3000/api/datasources/proxy/uid/tempo/api/search?q=<TraceQL>` and `.../proxy/uid/loki/loki/api/v1/query_range?query=<LogQL>`.
- OTel Python pins move in lockstep (SDK `1.36.0` with instrumentation `0.57b0`); bump them together.

# Task Manager – telemetry practice

## Environment
- Windows, Docker Desktop, PowerShell. Everything runs via Docker Compose.
- Services: web (Django 5.1, port 8000), worker (Celery 5), db (postgres:16-alpine, host port 5433), redis (redis:7-alpine), otel-collector (internal), tempo (internal), loki (internal), prometheus (9090), grafana (3000).
- Inside Docker, hosts are "db" and "redis". Settings come from .env.

## Rules
- NEVER run `docker compose down` (it once deleted a Redis container I made). Use `docker compose stop` / `up -d`.
- If a host port clashes, change only the host side (e.g. "6380:6379").
- Publish only Grafana (3000) and Prometheus (9090) to the host. Keep collector, Tempo and Loki internal.
- Do one phase at a time. Explain briefly WHY each piece is needed (I'm learning).
- After each phase, tell me which URL to open and what I should see.
- Never use user_id, task_id or raw URLs as metric labels.
- Celery uses prefork: initialise OpenTelemetry in worker_process_init.
- Set retention: Prometheus 7d, Tempo and Loki 48h. Add mem_limit to the observability services.