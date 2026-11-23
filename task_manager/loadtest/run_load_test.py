"""
Load test + telemetry verification for the task manager.

Runs INSIDE the Docker network as a one-off container, so it can reach web, Prometheus,
Tempo and Loki by service name without publishing any extra ports:

    docker compose run --rm --no-deps -T -e OTEL_SERVICE_NAME=load-generator \
        web python -m loadtest.run_load_test --rps 10 --duration 10

Phases:
  1. preflight  - web, Prometheus, Tempo and Loki answer; telemetry is enabled
  2. baseline   - snapshot of the Prometheus counters before any traffic
  3. setup      - test users loadtest_a / loadtest_b and one seed task
  4. load       - open-loop schedule: request i starts at t0 + i/rps, whether or not
                  earlier requests have finished (avoids "coordinated omission")
  5. verify     - metrics, traces and logs checked IN PARALLEL, each polling until its
                  data has arrived or its timeout expires
  6. cleanup    - deletes the tasks created by this run (skip with --keep-data)
  7. report     - summary on stdout + JSON in loadtest/results/<run_id>.json
Exit code: 0 = every check passed, 1 = at least one check failed, 2 = preflight failed.

Each request is wrapped in a CLIENT span of service "load-generator" whose trace context
is sent in the W3C `traceparent` header. Django continues that trace, so the script knows
the exact trace id of every request and can look each one up in Tempo and Loki.
"""
import argparse
import json
import os
import random
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, StatusCode

from config.telemetry import setup_telemetry, shutdown_telemetry

WEB = os.environ.get("LOADTEST_TARGET", "http://web:8000")
PROM = "http://prometheus:9090"
TEMPO = "http://tempo:3200"
LOKI = "http://loki:3100"

USER_A, USER_B, PASSWORD = "loadtest_a", "loadtest_b", "loadtest-pass"
MISSING_TASK_ID = 999_999_999

# Django route patterns exactly as they appear in the Prometheus `http_route` label.
ROUTE = {
    "tasks": "api/tasks/$",
    "task": "api/tasks/(?P<pk>[^/.]+)/$",
    "users": "api/auth/users/",
    "signup": "api/auth/signup/",
    "login": "api/auth/login/",
}

# Request mix per 20 requests (repeated to fill the run). Weights sum to 20.
MIX = [
    ("list_tasks", 8),       # GET  /api/tasks/                         40%
    ("list_users", 4),       # GET  /api/auth/users/  (Redis cache)     20%
    ("get_task", 3),         # GET  /api/tasks/{seed}/                  15%
    ("create_assigned", 3),  # POST /api/tasks/ -> Celery job (web+worker trace)  15%
    ("update_task", 1),      # PATCH /api/tasks/{seed}/ (name only)      5%
    ("missing_task", 1),     # GET  /api/tasks/999999999/ -> 404         5%
]

tracer = trace.get_tracer("loadtest")
_local = threading.local()


def session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


@dataclass
class Rec:
    phase: str
    op: str
    method: str
    template: str
    route: str
    status: int = 0
    latency_ms: float = 0.0
    start_lag_ms: float = 0.0
    trace_id: str = ""
    task_id: int = 0
    error: str = ""


def call(run_id, phase, op, method, template, route, path, token=None, body=None, scheduled=None):
    rec = Rec(phase=phase, op=op, method=method, template=template, route=route)
    if scheduled is not None:
        rec.start_lag_ms = max(0.0, (time.perf_counter() - scheduled) * 1000)
    with tracer.start_as_current_span(
        f"{method} {template}",
        kind=SpanKind.CLIENT,
        attributes={
            "http.request.method": method,
            "url.template": template,
            "loadtest.run_id": run_id,
            "loadtest.phase": phase,
            "loadtest.op": op,
        },
    ) as span:
        headers = {"Authorization": f"Token {token}"} if token else {}
        propagate.inject(headers)  # adds `traceparent` -> Django continues this trace
        t0 = time.perf_counter()
        data = None
        try:
            r = session().request(method, WEB + path, json=body, headers=headers, timeout=15)
            rec.status = r.status_code
            if "json" in r.headers.get("Content-Type", ""):
                data = r.json()
        except Exception as exc:  # network error, timeout
            rec.error = f"{type(exc).__name__}: {exc}"[:200]
        rec.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        span.set_attribute("http.response.status_code", rec.status)
        if rec.status == 0 or rec.status >= 500:
            span.set_status(StatusCode.ERROR)
        ctx = span.get_span_context()
        if ctx.is_valid:
            rec.trace_id = format(ctx.trace_id, "032x")
    return rec, data


# --------------------------------------------------------------------------- phases
def preflight(wait_s=60):
    """Every dependency must answer 200. Readiness can be briefly 503 (Tempo/Loki report
    "not ready" for a while after their internal ring changes), so retry for up to
    `wait_s` seconds before giving up, and show the service's own reason if it never recovers."""
    checks = {
        # /metrics is in OTEL_PYTHON_DJANGO_EXCLUDED_URLS, so this probe doesn't show up
        # in the Prometheus counters (a /login/ probe did, as an "extra traffic" row,
        # because it was exported after the baseline snapshot).
        "web": WEB + "/metrics",
        "prometheus": PROM + "/-/ready",
        "tempo": TEMPO + "/ready",
        "loki": LOKI + "/ready",
    }
    deadline = time.time() + wait_s
    pending = dict(checks)
    last = {}
    while pending:
        for name, url in list(pending.items()):
            try:
                r = requests.get(url, timeout=5)
                if r.status_code == 200:
                    pending.pop(name)
                    if name in last:
                        print(f"preflight: {name} became ready after retrying", flush=True)
                else:
                    last[name] = f"HTTP {r.status_code}: {r.text.strip()[:150]}"
            except Exception as exc:
                last[name] = f"unreachable: {type(exc).__name__}"
        if not pending or time.time() > deadline:
            break
        time.sleep(2)
    problems = [f"{name} not ready after {wait_s}s ({checks[name]}) - {last.get(name, '?')}" for name in pending]
    if not trace.get_tracer_provider().__class__.__name__.startswith("TracerProvider"):
        problems.append("telemetry disabled: OTEL_EXPORTER_OTLP_ENDPOINT not set")
    return problems


def ensure_user(run_id, records, username):
    rec, data = call(run_id, "setup", "signup", "POST", "/api/auth/signup/", ROUTE["signup"],
                     "/api/auth/signup/", body={"username": username, "password": PASSWORD})
    records.append(rec)
    if rec.status != 201:  # already exists -> log in
        rec, data = call(run_id, "setup", "login", "POST", "/api/auth/login/", ROUTE["login"],
                         "/api/auth/login/", body={"username": username, "password": PASSWORD})
        records.append(rec)
    if rec.status not in (200, 201) or not data:
        raise RuntimeError(f"could not sign up / log in {username}: HTTP {rec.status} {rec.error}")
    return data["token"], data["user"]["id"]


def build_plan(n, seed):
    pattern = [op for op, weight in MIX for _ in range(weight)]
    plan = [pattern[i % len(pattern)] for i in range(n)]
    random.Random(seed).shuffle(plan)
    return plan


def run_op(run_id, i, op, ctx, scheduled):
    tok = ctx["token_a"]
    seed = ctx["seed_id"]
    if op == "list_tasks":
        return call(run_id, "load", op, "GET", "/api/tasks/", ROUTE["tasks"], "/api/tasks/", tok, scheduled=scheduled)[0]
    if op == "list_users":
        return call(run_id, "load", op, "GET", "/api/auth/users/", ROUTE["users"], "/api/auth/users/", tok, scheduled=scheduled)[0]
    if op == "get_task":
        return call(run_id, "load", op, "GET", "/api/tasks/{id}/", ROUTE["task"], f"/api/tasks/{seed}/", tok, scheduled=scheduled)[0]
    if op == "missing_task":
        return call(run_id, "load", op, "GET", "/api/tasks/{id}/", ROUTE["task"], f"/api/tasks/{MISSING_TASK_ID}/", tok, scheduled=scheduled)[0]
    if op == "update_task":
        return call(run_id, "load", op, "PATCH", "/api/tasks/{id}/", ROUTE["task"], f"/api/tasks/{seed}/", tok,
                    body={"task_name": f"loadtest seed {run_id} #{i}"}, scheduled=scheduled)[0]
    if op == "create_assigned":
        rec, data = call(run_id, "load", op, "POST", "/api/tasks/", ROUTE["tasks"], "/api/tasks/", tok,
                         body={"task_name": f"loadtest {run_id} #{i}", "assigned_to": ctx["user_b_id"]},
                         scheduled=scheduled)
        if rec.status == 201 and data:
            rec.task_id = data["id"]
        return rec
    raise ValueError(op)


def run_load(run_id, rps, duration, ctx):
    n = int(rps * duration)
    plan = build_plan(n, run_id)
    interval = 1.0 / rps
    records = []
    futures = []
    # Enough threads that a slow response never delays the next scheduled start.
    with ThreadPoolExecutor(max_workers=min(64, max(8, rps * 4))) as pool:
        t0 = time.perf_counter() + 0.2
        for i, op in enumerate(plan):
            target = t0 + i * interval
            while True:  # precise wait: sleep most of the gap, then spin briefly
                left = target - time.perf_counter()
                if left <= 0:
                    break
                time.sleep(left - 0.002 if left > 0.003 else 0)
            futures.append(pool.submit(run_op, run_id, i, op, ctx, target))
        send_window = time.perf_counter() - t0 + interval  # time spent *starting* requests
        wait(futures)
        elapsed = time.perf_counter() - t0                 # including the last responses
    return [f.result() for f in futures], send_window, elapsed


# --------------------------------------------------------------------------- verification
def prom_query(q):
    r = requests.get(PROM + "/api/v1/query", params={"query": q}, timeout=10)
    r.raise_for_status()
    return r.json()["data"]["result"]


def http_counts():
    q = ('sum by (http_route, http_request_method, http_response_status_code) '
         '(http_server_request_duration_seconds_count{job="task-manager-web"})')
    out = {}
    for s in prom_query(q):
        m = s["metric"]
        key = (m.get("http_route", ""), m.get("http_request_method", ""), m.get("http_response_status_code", ""))
        out[key] = float(s["value"][1])
    return out


def celery_count():
    q = ('sum(flower_task_runtime_seconds_count{job="task-manager-worker",'
         'task="tasks.tasks.send_assignment_notification"})')
    res = prom_query(q)
    return float(res[0]["value"][1]) if res else 0.0


def verify_metrics(baseline, records, timeout):
    """Counter deltas in Prometheus must equal what the client actually sent."""
    expected = Counter((r.route, r.method, str(r.status)) for r in records
                       if r.phase in ("setup", "load") and r.status)
    expected_celery = sum(1 for r in records if r.op == "create_assigned" and r.status == 201)
    deadline = time.time() + timeout
    while True:
        now, cel = http_counts(), celery_count()
        delta = {k: now.get(k, 0) - baseline["http"].get(k, 0) for k in set(now) | set(expected)}
        cel_delta = cel - baseline["celery"]
        complete = all(delta.get(k, 0) >= v for k, v in expected.items()) and cel_delta >= expected_celery
        if complete or time.time() > deadline:
            break
        time.sleep(5)
    rows, ok = [], True
    for k in sorted(set(expected) | {k for k, v in delta.items() if v > 0}):
        exp, got = expected.get(k, 0), int(delta.get(k, 0))
        status = "ok" if got == exp else ("extra traffic" if got > exp else "MISSING")
        if got < exp:
            ok = False
        rows.append({"route": k[0] or "(no route)", "method": k[1], "status": k[2],
                     "expected": exp, "observed": got, "result": status})
    cel_ok = cel_delta >= expected_celery
    return {
        "ok": ok and cel_ok,
        "http": rows,
        "celery_tasks": {"expected": expected_celery, "observed": int(cel_delta),
                         "result": "ok" if cel_delta == expected_celery else ("extra" if cel_ok else "MISSING")},
    }


def fetch_trace(trace_id):
    r = requests.get(f"{TEMPO}/api/v2/traces/{trace_id}", timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    spans = []
    for rs in r.json().get("trace", {}).get("resourceSpans", []):
        svc = next((a["value"].get("stringValue") for a in rs["resource"]["attributes"]
                    if a["key"] == "service.name"), "?")
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                spans.append((svc, sp["name"]))
    return spans


def trace_complete(rec, spans):
    if not spans:
        return False
    services = {s for s, _ in spans}
    if not {"load-generator", "task-manager-web"} <= services:
        return False
    if rec.op == "create_assigned" and rec.status == 201:
        return any(s == "task-manager-worker" and n.startswith("run/") for s, n in spans)
    return True


def verify_traces(records, timeout):
    """Every load request's trace must be in Tempo; task creations must reach the worker."""
    todo = {r.trace_id: r for r in records if r.phase == "load" and r.trace_id}
    found = {}
    deadline = time.time() + timeout
    # Spans need ~5-10 s (batch export + collector batch + Tempo ingest) to become
    # queryable, so polling earlier only repeats lookups. Each trace-by-id lookup makes
    # Tempo scan its blocks; 10 in parallel, repeated, got Tempo OOM-killed at its
    # 512 MiB limit during a 300-request run. Keep it gentle.
    time.sleep(8)
    with ThreadPoolExecutor(max_workers=4) as pool:
        while todo and time.time() < deadline:
            ids = list(todo)
            for tid, spans in zip(ids, pool.map(fetch_trace, ids)):
                if trace_complete(todo[tid], spans):
                    found[tid] = spans
                    todo.pop(tid)
            if todo:
                time.sleep(3)
    creates = [r for r in records if r.phase == "load" and r.op == "create_assigned" and r.status == 201]
    cross = sum(1 for r in creates if r.trace_id in found)
    span_counts = [len(s) for s in found.values()]
    example = next((r.trace_id for r in creates if r.trace_id in found), None)
    return {
        "ok": not todo,
        "complete": len(found),
        "expected": len(found) + len(todo),
        "cross_service": {"expected": len(creates), "observed": cross},
        "avg_spans_per_trace": round(sum(span_counts) / len(span_counts), 1) if span_counts else 0,
        "max_spans_per_trace": max(span_counts) if span_counts else 0,
        "missing_or_incomplete": [{"trace_id": t, "op": r.op} for t, r in list(todo.items())[:10]],
        "example_cross_service_trace": example,
        "example_trace_spans": sorted(found[example], key=lambda x: x[0]) if example else [],
    }


def loki_lines(start_ns):
    params = {
        "query": '{service_name=~"task-manager-.+"}',
        "start": str(start_ns),
        "end": str(time.time_ns()),
        "limit": "5000",
        "direction": "forward",
    }
    r = requests.get(LOKI + "/loki/api/v1/query_range", params=params, timeout=15)
    r.raise_for_status()
    lines = []
    for stream in r.json()["data"]["result"]:
        labels = stream["stream"]  # labels + structured metadata (merged)
        for _ts, line in stream["values"]:
            lines.append((labels.get("service_name", ""), labels.get("trace_id", ""),
                          labels.get("severity_text", labels.get("detected_level", "")), line))
    return lines


def verify_logs(records, start_ns, timeout):
    """For every created task: web 'created' line + worker 'NOTIFICATION' + 'succeeded',
    all carrying the request's trace_id."""
    creates = [r for r in records if r.phase == "load" and r.op == "create_assigned" and r.status == 201]
    deadline = time.time() + timeout
    while True:
        lines = loki_lines(start_ns)
        by_trace = defaultdict(list)
        for svc, tid, sev, line in lines:
            by_trace[tid].append((svc, sev, line))
        missing = []
        for r in creates:
            got = by_trace.get(r.trace_id, [])
            need = {
                "web 'Task N created'": any(s == "task-manager-web" and f"Task {r.task_id} created" in l for s, _, l in got),
                "worker NOTIFICATION": any(s == "task-manager-worker" and "NOTIFICATION" in l for s, _, l in got),
                "worker succeeded": any(s == "task-manager-worker" and "succeeded" in l for s, _, l in got),
            }
            if not all(need.values()):
                missing.append({"task_id": r.task_id, "trace_id": r.trace_id,
                                "missing": [k for k, v in need.items() if not v]})
        if not missing or time.time() > deadline:
            break
        time.sleep(3)
    run_ids = {r.trace_id for r in records if r.trace_id}
    return {
        "ok": not missing,
        "tasks_with_all_3_lines": len(creates) - len(missing),
        "tasks_expected": len(creates),
        "lines_in_window": len(lines),
        "lines_with_this_runs_trace_ids": sum(1 for _, tid, _, _ in lines if tid in run_ids),
        "lines_without_trace_id": sum(1 for _, tid, _, _ in lines if not tid),
        "error_lines": [l[:200] for _, _, sev, l in lines if sev.upper() == "ERROR"][:10],
        "incomplete": missing[:10],
    }


# --------------------------------------------------------------------------- reporting
def pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return round(s[f] + (s[c] - s[f]) * (k - f), 1)


def client_stats(load, send_window, elapsed, rps, duration):
    lat = [r.latency_ms for r in load if r.status]
    per_op = {}
    for op in sorted({r.op for r in load}):
        v = [r.latency_ms for r in load if r.op == op and r.status]
        per_op[op] = {"count": sum(1 for r in load if r.op == op),
                      "statuses": dict(Counter(str(r.status) for r in load if r.op == op)),
                      "p50_ms": pct(v, 50), "p95_ms": pct(v, 95), "max_ms": round(max(v), 1) if v else 0}
    unexpected = [r for r in load if r.status == 0 or r.status >= 500
                  or (r.op != "missing_task" and r.status >= 400)
                  or (r.op == "missing_task" and r.status != 404)]
    return {
        "ok": not unexpected,
        "target_rps": rps,
        "duration_s": duration,
        "requests": len(load),
        "achieved_rps": round(len(load) / send_window, 2) if send_window else 0,
        "wall_time_s": round(elapsed, 2),
        "status_codes": dict(Counter(str(r.status) for r in load)),
        "latency_ms": {"p50": pct(lat, 50), "p95": pct(lat, 95), "p99": pct(lat, 99),
                       "max": round(max(lat), 1) if lat else 0},
        "max_start_lag_ms": round(max((r.start_lag_ms for r in load), default=0), 1),
        "per_op": per_op,
        "unexpected": [{"op": r.op, "status": r.status, "error": r.error} for r in unexpected[:10]],
    }


def print_report(rep):
    c, m, t, l = rep["client"], rep["metrics"], rep["traces"], rep["logs"]
    mark = lambda ok: "PASS" if ok else "FAIL"  # noqa: E731
    print("\n" + "=" * 72)
    print(f"LOAD TEST {rep['run_id']}   {rep['started_utc']} -> {rep['finished_utc']}")
    print("=" * 72)
    print(f"[{mark(c['ok'])}] CLIENT  {c['requests']} requests sent at {c['achieved_rps']} req/s "
          f"(target {c['target_rps']}), all answered after {c['wall_time_s']}s, "
          f"max start lag {c['max_start_lag_ms']} ms")
    print(f"        status codes {c['status_codes']}  latency p50 {c['latency_ms']['p50']} / "
          f"p95 {c['latency_ms']['p95']} / p99 {c['latency_ms']['p99']} / max {c['latency_ms']['max']} ms")
    for op, s in c["per_op"].items():
        print(f"          {op:<16} n={s['count']:<3} {s['statuses']}  p50 {s['p50_ms']} ms  p95 {s['p95_ms']} ms")
    print(f"[{mark(m['ok'])}] METRICS Prometheus counter deltas vs requests sent (setup + load):")
    for row in m["http"]:
        print(f"          {row['method']:<6} {row['route']:<28} {row['status']:<4} expected {row['expected']:<3} "
              f"observed {row['observed']:<3} {row['result']}")
    ct = m["celery_tasks"]
    print(f"          celery send_assignment_notification   expected {ct['expected']:<3} observed {ct['observed']:<3} {ct['result']}")
    print(f"[{mark(t['ok'])}] TRACES  {t['complete']}/{t['expected']} complete in Tempo; web->worker "
          f"{t['cross_service']['observed']}/{t['cross_service']['expected']}; avg {t['avg_spans_per_trace']} spans "
          f"(max {t['max_spans_per_trace']})")
    if t["example_cross_service_trace"]:
        print(f"          example trace: {t['example_cross_service_trace']}")
    print(f"[{mark(l['ok'])}] LOGS    {l['tasks_with_all_3_lines']}/{l['tasks_expected']} tasks have web+worker lines "
          f"sharing the trace_id; {l['lines_with_this_runs_trace_ids']} lines carry this run's trace ids; "
          f"{l['lines_without_trace_id']} lines without trace_id; {len(l['error_lines'])} ERROR lines")
    print(f"CLEANUP {rep['cleanup']}")
    print(f"\nVERDICT: {'ALL CHECKS PASSED' if rep['ok'] else 'SOME CHECKS FAILED'}")
    print(f"Tempo (TraceQL): {{ resource.service.name = \"load-generator\" && span.loadtest.run_id = \"{rep['run_id']}\" }}")
    print(f"Loki  (LogQL):   {{service_name=~\"task-manager-.+\"}} |= \"loadtest {rep['run_id']}\"")
    print(f"Grafana time range (ms): from={rep['grafana_from_ms']} to={rep['grafana_to_ms']}")
    print(f"Full JSON: {rep['results_file']}")


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rps", type=int, default=10)
    ap.add_argument("--duration", type=int, default=10, help="seconds")
    ap.add_argument("--keep-data", action="store_true", help="don't delete the tasks created by this run")
    ap.add_argument("--verify-timeout", type=int, default=90, help="max seconds to wait for telemetry")
    args = ap.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:4]
    setup_telemetry()  # tracer for the CLIENT spans (service.name from OTEL_SERVICE_NAME)

    problems = preflight()
    if problems:
        print("PREFLIGHT FAILED:\n  - " + "\n  - ".join(problems))
        sys.exit(2)

    started = datetime.now(timezone.utc)
    window_start_ns = time.time_ns() - 5_000_000_000
    baseline = {"http": http_counts(), "celery": celery_count()}

    records = []
    token_a, _ = ensure_user(run_id, records, USER_A)
    _, user_b_id = ensure_user(run_id, records, USER_B)
    seed_rec, seed = call(run_id, "setup", "seed_task", "POST", "/api/tasks/", ROUTE["tasks"], "/api/tasks/",
                          token_a, body={"task_name": f"loadtest seed {run_id}"})
    records.append(seed_rec)
    if seed_rec.status != 201:
        print(f"SETUP FAILED: seed task HTTP {seed_rec.status} {seed_rec.error}")
        sys.exit(2)
    ctx = {"token_a": token_a, "user_b_id": user_b_id, "seed_id": seed["id"]}

    print(f"run {run_id}: {args.rps} req/s x {args.duration}s against {WEB} ...", flush=True)
    load, send_window, elapsed = run_load(run_id, args.rps, args.duration, ctx)
    records += load
    trace.get_tracer_provider().force_flush()  # client spans must reach Tempo before we look

    print("load finished; verifying metrics, traces and logs (in parallel) ...", flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        fm = pool.submit(verify_metrics, baseline, records, args.verify_timeout)
        ft = pool.submit(verify_traces, records, args.verify_timeout)
        fl = pool.submit(verify_logs, records, window_start_ns, args.verify_timeout)
        metrics_r, traces_r, logs_r = fm.result(), ft.result(), fl.result()

    if args.keep_data:
        cleanup = "skipped (--keep-data)"
    else:
        ids = [r.task_id for r in load if r.task_id] + [seed["id"]]
        deleted = sum(
            1 for tid in ids
            if call(run_id, "cleanup", "delete_task", "DELETE", "/api/tasks/{id}/", ROUTE["task"],
                    f"/api/tasks/{tid}/", token_a)[0].status == 204
        )
        cleanup = f"deleted {deleted}/{len(ids)} tasks created by this run (users loadtest_a/b are kept for reuse)"

    finished = datetime.now(timezone.utc)
    client_r = client_stats(load, send_window, elapsed, args.rps, args.duration)
    results_dir = Path(__file__).resolve().parent / "results"
    results_dir.mkdir(exist_ok=True)
    out_file = results_dir / f"{run_id}.json"
    rep = {
        "run_id": run_id,
        "started_utc": started.isoformat(timespec="seconds"),
        "finished_utc": finished.isoformat(timespec="seconds"),
        "grafana_from_ms": int(started.timestamp() * 1000) - 30_000,
        "grafana_to_ms": int(finished.timestamp() * 1000) + 30_000,
        "ok": all(x["ok"] for x in (client_r, metrics_r, traces_r, logs_r)),
        "client": client_r,
        "metrics": metrics_r,
        "traces": traces_r,
        "logs": logs_r,
        "cleanup": cleanup,
        "results_file": f"loadtest/results/{run_id}.json",
        "requests": [asdict(r) for r in records],
    }
    out_file.write_text(json.dumps(rep, indent=2))
    print_report(rep)
    shutdown_telemetry()
    sys.exit(0 if rep["ok"] else 1)


if __name__ == "__main__":
    main()
