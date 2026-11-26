# Observability

Source: [`backend/observability.py`](../backend/observability.py). This page
lists every metric the backend can export, the complete set of labels
(OpenTelemetry attributes) each one may carry, which of them are recorded
today, and the exporters and switches that send traces and metrics out. The
Google Cloud alert policies and dashboard built on these metrics are
described at the end.

`backend/test_metric_labels.py` checks that this page, the `METRIC_LABELS`
catalogue in the code and the labels actually recorded agree.

- [Exporters and switches](#exporters-and-switches)
- [Metric catalogue](#metric-catalogue)
- [Where the metrics are recorded](#where-the-metrics-are-recorded)
- [Traces](#traces)
- [Cardinality rules](#cardinality-rules)
- [Cloud Monitoring alerts and dashboard](#cloud-monitoring-alerts-and-dashboard)

## Exporters and switches

OpenTelemetry is active only when every package `observability.py` imports
is installed (the `server` extra). Otherwise the module runs in no-op mode:
the helpers can be called, but nothing is recorded or exported.

The API calls `init_observability` at startup (`main.py`). A Celery worker
process does the same after it forks, through the hooks that
`install_celery_worker_hooks()` registers; `celery_app.py` calls it at
import. The hooks use Celery's `worker_process_init` signal, which only the
prefork pool (the default) sends, so a worker started with `--pool solo` or
`--pool threads` exports nothing.

| Exporter | Signal | API | Worker | Switch |
| --- | --- | --- | --- | --- |
| Console | traces | when `ENVIRONMENT` is not `production` | never | none |
| OTLP (gRPC) | traces, metrics (every 30 s) | yes | yes | `OTEL_EXPORTER_OTLP_ENDPOINT` set |
| Prometheus reader | metrics | yes | never | none |
| Cloud Trace | traces | yes | yes | `OTEL_CLOUD_TRACE_ENABLED` |
| Cloud Monitoring | metrics (every 60 s) | yes | yes | `OTEL_CLOUD_MONITORING_ENABLED` |

- The OTLP exporters connect with `insecure=True` (no TLS). Point
  `OTEL_EXPORTER_OTLP_ENDPOINT` at a collector on the same host or private
  network.
- The Prometheus reader registers the metrics in the process, but the
  backend serves no scrape endpoint, so nothing reads it unless you add one.
  (`/admin/metrics` is an application route for the dashboard, not a
  Prometheus endpoint.)
- `OTEL_CLOUD_TRACE_ENABLED` and `OTEL_CLOUD_MONITORING_ENABLED` are on for
  `1`, `true`, `yes` or `on` and off otherwise, which is the default. Both
  authenticate with application default credentials and write to
  `GOOGLE_CLOUD_PROJECT` when it is set, otherwise to the project of the
  credentials. The account needs `roles/cloudtrace.agent` for traces and
  `roles/monitoring.metricWriter` for metrics. A missing package or missing
  credentials is logged once and the process keeps running without that
  exporter.
- A worker process creates a meter provider only when at least one metric
  exporter is configured (OTLP or Cloud Monitoring). Without one, the worker
  records nothing.

On Google Cloud, Terraform sets the two Cloud switches
(`cloud_trace_enabled`, API only; `cloud_monitoring_enabled`, API and
worker) and grants the roles. Both are off by default. See
[DEPLOYMENT.md, Cloud Trace](DEPLOYMENT.md#cloud-trace) and
[Metrics and alerts](DEPLOYMENT.md#metrics-and-alerts).

## Metric catalogue

Every metric has an allowlist of label names: `METRIC_LABELS` and
`ALERTING_METRIC_LABELS` in `observability.py`, merged by
`all_metric_labels()`. The two dictionaries are separate only for historical
reasons; they act as one allowlist.

### Application metrics

| Metric | Type | Labels | Recorded today |
| --- | --- | --- | --- |
| `agent_runs_total` | counter | `mode`, `outcome` | yes, worker |
| `agent_run_duration_seconds` | histogram (s) | `mode`, `outcome` | yes, worker |
| `agent_runs_active` | up-down counter | `mode` | no |
| `llm_tokens_total` | counter | `model` | yes, worker |
| `llm_phase_tokens_total` | counter | `model`, `phase`, `kind` | yes, worker |
| `llm_estimated_cost_usd_total` | counter (unit `{USD}`) | `model`, `phase` | yes, worker |
| `llm_request_duration_seconds` | histogram (s) | `model`, `outcome` | no |
| `github_api_calls_total` | counter | `operation`, `outcome` | no |
| `celery_queue_depth` | gauge | `queue` | no |
| `celery_task_duration_seconds` | histogram (s) | `task_name`, `queue`, `outcome` | no |
| `celery_worker_heartbeat` | observable gauge, value 1 | none | yes, every worker process |

"No" means a `record_*` helper exists but no code calls it yet. Such a
metric is never written, so no exporter shows it.

Label values:

| Label | Values |
| --- | --- |
| `mode` | `autonomous`, `targeted_issue`, `code_review`, `refactor`; anything else is `other` |
| `outcome` (runs) | `completed`, `failed`, `cancelled`, `timeout`; anything else is `other`. `timeout` is a run stopped by the 30-minute soft time limit. It is stored with status `failed` (`runs.status` has no timeout value) |
| `outcome` (other metrics) | `success`, `failure` |
| `phase` | `architect`, `brainstormer`, `visionary`, `pm`, `reviewer`, `implementer`, `maintainer`, `system`; anything else is `other` |
| `kind` | `input`, `output`, `total`; anything else is `other`. Today always `total` (see below) |
| `model`, `operation`, `queue`, `task_name` | must look like an identifier: a letter or digit first, then letters, digits and `_ . : -`, at most 64 characters in all, so no `/`, spaces or sentences; anything else is `other` |

`agent_run_duration_seconds` uses explicit buckets suited to runs of up to
30 minutes: 10, 30, 60, 120, 300, 600, 900, 1200, 1500, 1800, 2400 and
3600 seconds.

Labels removed by #404: `repo_name` (agent runs), `run_id` (LLM tokens),
`repo` (GitHub API calls). The run `status` label is now called `outcome`.

### Estimated LLM cost

`llm_estimated_cost_usd_total` is an estimate, not a bill. It prices tokens
with a table in `observability.py` (USD per million tokens):

| Model | Input | Output |
| --- | --- | --- |
| `llama-3.3-70b-versatile` | 0.59 | 0.79 |
| `llama-3.1-8b-instant` | 0.05 | 0.08 |

These are Groq's earlier public list prices; they could not be confirmed
against Groq's current price page. `LLM_PRICES_JSON` adds or replaces
entries, for example
`{"llama-3.3-70b-versatile": {"input_per_mtok": 0.59, "output_per_mtok": 0.79}}`.
An invalid value is logged once and ignored. A model in neither table adds
tokens but no cost, so the metric undercounts rather than invents a price.

The engine reports one token count per LLM call, not split into prompt and
completion. The worker therefore records `kind="total"` and prices the
count at the model's higher rate, an upper bound. For
`llama-3.3-70b-versatile` that overstates the cost by at most about 34 %.

### HTTP metrics (instrumentation libraries)

FastAPI requests are measured by `opentelemetry-instrumentation-fastapi`
(API only), and outgoing calls by the `httpx` and `requests`
instrumentations. Their names depend on the semantic-convention mode
(`OTEL_SEMCONV_STABILITY_OPT_IN`, read by the instrumentation libraries), so
both the default and the stable names are catalogued.

| Metric | Labels kept |
| --- | --- |
| `http.server.duration`, `http.server.request.duration`, `http.server.request.size`, `http.server.request.body.size`, `http.server.response.size`, `http.server.response.body.size` | `http.method`, `http.status_code`, `http.target`, `http.request.method`, `http.response.status_code`, `http.route` |
| `http.server.active_requests` | `http.method`, `http.request.method` |
| `http.client.duration`, `http.client.request.duration` | `http.method`, `http.status_code`, `net.peer.name`, `http.request.method`, `http.response.status_code`, `server.address` |

- In the default conventions the FastAPI instrumentation puts the **route
  template** in `http.target` (`/runs/{run_id}`) and omits it for a request
  that matches no route; it never records the raw path or query string. The
  stable conventions use `http.route`. `backend/test_metric_labels.py`
  drives a real instrumented app with an id in the path and checks this.
- `/healthz`, `/health` and `/metrics` are excluded from the FastAPI
  instrumentation.
- Dropped by the Views: `http.server_name` (copied from the client's `Host`
  header, so any caller could create new series), `http.host`,
  `net.host.port`, `http.scheme` and `http.flavor`.
- HTTP methods are normalized by the instrumentation (unknown methods become
  `_OTHER`). Status codes are kept as codes rather than classes because they
  are already a small set.
- Client `net.peer.name` / `server.address` is the host of a configured
  service (GitHub, Supabase, Groq); full URLs are never labels.

## Where the metrics are recorded

All application metrics that are recorded today come from the Celery
worker. The API exports only the HTTP metrics.

- `agent_runs_total` and `agent_run_duration_seconds`:
  `_execute_agent_run` in `backend/tasks.py`, once per run that acquired the
  repository lock, when the run ends. A run that gave up waiting for the
  lock, or a redelivered run that had already finished, is not counted. The
  duration starts when the lock is held, so time spent queued is excluded.
  A run that ends without setting an outcome counts as `failed`.
- `llm_tokens_total`, `llm_phase_tokens_total` and
  `llm_estimated_cost_usd_total`: `MetricsEmitter` in
  `backend/telemetry_metrics.py`, from each `TelemetryEvent` the engine
  emits during a run started by `tasks.py`. The SDK package
  (`backend/automaintainer`) never imports `observability`; applications
  that embed the engine bring their own telemetry.
- `celery_worker_heartbeat`: every prefork worker process reports 1 at each
  export. The series is absent when no worker process runs.
- HTTP server metrics: the API, through `instrument_fastapi`. HTTP client
  metrics: any process where `init_observability` ran.

Recording never changes how a run ends: errors in the metric helpers are
logged and swallowed.

## Traces

| Source | Process | Span |
| --- | --- | --- |
| FastAPI instrumentation | API | one server span per request (excluding `/healthz`, `/health`, `/metrics`) |
| `httpx`, `requests` and `redis` instrumentations | API and worker | client spans for outgoing calls |
| ADK Reviewer (`REVIEWER_BACKEND=adk`) | worker | `specialist.reviewer` per review, `specialist.tool` per tool call |

`trace_agent_execution`, `trace_llm_call`, `trace_github_api` and
`trace_celery_task` exist in `observability.py` but nothing calls them yet,
so there is no span per agent run.

The worker has no console exporter. Its spans leave the process only through
OTLP or Cloud Trace. On Google Cloud, Terraform sets
`OTEL_CLOUD_TRACE_ENABLED` on the API only, so only the API's spans reach
Cloud Trace.

The API's log lines carry the current `trace_id` and `span_id`, or zeros
outside a span (`setup_structured_logging`, called in `main.py`).

## Cardinality rules

Every distinct combination of label values is a separate time series. A
label whose value comes from a repository name, a run id, a user or
organization id, a raw URL path, a `Host` header or an error message grows
without limit, can exhaust the metrics backend, and leaks identifiers into
it. So:

- Metric labels take a small, known set of values: a phase or mode, an
  outcome, an HTTP status code, a **route template** (`/runs/{run_id}`,
  never `/runs/3f1c...`), a configured model or queue name, an operation
  name that is a code constant.
- High-cardinality identifiers (`repo_name`, `run_id`, user ids, paths,
  error text) go in **logs and span attributes only**.

Two mechanisms enforce this:

1. The `record_*` helpers build their labels themselves and normalize every
   value that can come from a request, as listed under
   [Label values](#application-metrics).
2. `init_observability` installs one OpenTelemetry `View` per catalogued
   metric (`metric_views()`) that keeps only the catalogued label names. A
   label that is not listed is dropped when it is recorded, whether it comes
   from this code or from an instrumentation library.

The SDK also caps every metric stream at 2,000 attribute sets by default, as
a last-resort safety net.

Where the identifiers go instead:

| Identifier | Where to find it |
| --- | --- |
| Run id, repository | Run logs (`logs` table), the `runs` row, Celery task arguments, worker log lines |
| Raw request path, `Host` | Spans from the FastAPI instrumentation |
| Error messages | Logs, `runs.error_message`, span events (`record_exception`) |

**Series per process in Cloud Monitoring.** The Cloud Monitoring exporter
writes each metric as `workload.googleapis.com/<name>` on the `generic_task`
resource: `job` is the service name (`automaintainer-backend` for the API,
`automaintainer-worker` for a worker process) and `task_id` identifies one
process. The exporter also adds an `opentelemetry_id` label. Every process
therefore writes its own series, and a restarted process starts new ones.
Celery replaces a worker child process after 10 tasks
(`worker_max_tasks_per_child` in `celery_app.py`), which also starts new
series. Alerts and charts must sum across series, as the Terraform ones do.

**Adding a metric or a label.** Add it to `METRIC_LABELS` (or
`ALERTING_METRIC_LABELS`), to this page and to the expected sets in
`backend/test_metric_labels.py`, and state why its values are bounded. If a
Cloud Monitoring alert or chart uses it, `backend/tests/test_gcp_monitoring_config.py`
checks the filter against the catalogue.

## Cloud Monitoring alerts and dashboard

[`infra/terraform/monitoring.tf`](../infra/terraform/monitoring.tf) creates
these when the Google Cloud deployment is applied. The application alerts
are off by default; see
[DEPLOYMENT.md, Metrics and alerts](DEPLOYMENT.md#metrics-and-alerts) for
when to turn them on.

| Alert policy | Metric | Condition (default threshold) | Created |
| --- | --- | --- | --- |
| API 5xx ratio | `run.googleapis.com/request_count` (Cloud Run) | 5xx / all requests over 5 minutes above `alert_api_5xx_ratio` (0.05), for 5 minutes | always |
| Agent run failure rate | `agent_runs_total` | outcome `failed` or `timeout` / all finished runs over 1 hour above `alert_run_failure_rate` (0.5) | `monitoring_app_alerts_enabled = true` |
| p95 agent run duration | `agent_run_duration_seconds` | 95th percentile over 1 hour above `alert_run_duration_p95_seconds` (1500) | `monitoring_app_alerts_enabled = true` |
| Estimated LLM cost per day | `llm_estimated_cost_usd_total` | sum over 24 hours above `alert_llm_cost_per_day_usd` (10) | `monitoring_app_alerts_enabled = true` |
| Celery worker heartbeat absent | `celery_worker_heartbeat` | no series for `alert_worker_heartbeat_absent_seconds` (600) | `monitoring_app_alerts_enabled = true` |

Every policy notifies the e-mail channel created from
`monitoring_alert_email`, if one is set; without it, incidents still open in
the Cloud console. The cost alert uses a 24-hour alignment period, within the
25-hour limit Cloud Monitoring allows for alerting.

The dashboard `<name_prefix> operations` (`terraform output
monitoring_dashboard_url`) has six charts, each aligned over 5 minutes:

| Chart | Metric | Grouped by |
| --- | --- | --- |
| Agent runs finished, by outcome | `agent_runs_total` | `outcome` |
| Agent run duration p95 (s), by outcome | `agent_run_duration_seconds` | `outcome` |
| LLM tokens, by phase and kind | `llm_phase_tokens_total` | `phase`, `kind` |
| Estimated LLM cost (USD, an estimate), by model | `llm_estimated_cost_usd_total` | `model` |
| Live Celery worker processes | `celery_worker_heartbeat` | none (sum) |
| API requests, by response class | `run.googleapis.com/request_count` | `response_code_class` |

The application charts stay empty until `cloud_monitoring_enabled` is on and
the worker has written the metric.
