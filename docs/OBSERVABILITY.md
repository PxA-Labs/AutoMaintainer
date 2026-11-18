# Observability: metric catalogue

Source: `backend/observability.py`. This page lists every metric the backend
exports and the complete set of labels (OpenTelemetry attributes) each one may
carry. `backend/test_metric_labels.py` checks that this page, the
`METRIC_LABELS` catalogue in the code and the labels actually recorded agree.

## The rule: bounded labels only

Every distinct combination of label values is a separate time series. A label
whose value comes from a repository name, a run id, a user or organization id,
a raw URL path, a `Host` header or an error message grows without limit and can
exhaust the metrics backend (and leaks identifiers into it). So:

- Metric labels take a small, known set of values: a phase or mode, an
  outcome, an HTTP status code, a **route template** (`/runs/{run_id}`, never
  `/runs/3f1c...`), a configured model or queue name, an operation name that
  is a code constant.
- High-cardinality identifiers (`repo_name`, `run_id`, user ids, paths, error
  text) go in **logs and span attributes only**. `trace_agent_execution`, for
  example, puts the repository and run id on the span, not on
  `agent_runs_active`.

Two mechanisms enforce this:

1. The `record_*` helpers build their labels themselves and normalize values
   that can come from a request. `mode` must be one of `autonomous`,
   `targeted_issue`, `code_review`, `refactor`, and a run `outcome` one of
   `completed`, `failed`, `cancelled`, `timeout`; anything else is recorded
   as `other`.
   `model`, `operation`, `queue` and `task_name` must look like an identifier
   (letters, digits, `_ . : -`, at most 64 characters, so no `/`, spaces or
   sentences); anything else is recorded as `other`.
2. `init_observability` installs one OpenTelemetry `View` per catalogued
   metric (`metric_views()`) that keeps only the catalogued label names. A
   label that is not listed here is dropped when it is recorded, whether it
   comes from our code or from an instrumentation library.

To add a metric or a label: add it to `METRIC_LABELS`, to this page and to the
expected sets in `backend/test_metric_labels.py`, and say why its values are
bounded.

## Application metrics

Recorded through the helpers in `observability.py`.

| Metric | Type | Labels | Values |
| --- | --- | --- | --- |
| `agent_runs_total` | counter | `mode`, `outcome` | mode as above; outcome `completed` / `failed` / `cancelled` / `timeout` / `other`. `timeout` is a run stopped by the 30-minute soft time limit, stored with status `failed` (`runs.status` has no timeout value) |
| `agent_run_duration_seconds` | histogram (s) | `mode`, `outcome` | as above |
| `agent_runs_active` | up-down counter | `mode` | as above |
| `llm_tokens_total` | counter | `model` | configured model name |
| `llm_request_duration_seconds` | histogram (s) | `model`, `outcome` | outcome `success` / `failure` |
| `github_api_calls_total` | counter | `operation`, `outcome` | operation is a code constant such as `get_pull`; outcome `success` / `failure` |
| `celery_queue_depth` | gauge | `queue` | configured queue name |
| `celery_task_duration_seconds` | histogram (s) | `task_name`, `queue`, `outcome` | registered task name; outcome `success` / `failure` |

Labels removed by #404: `repo_name` (agent runs), `run_id` (LLM tokens),
`repo` (GitHub API calls). The run `status` label is now called `outcome`.

## HTTP metrics (instrumentation libraries)

FastAPI requests are measured by `opentelemetry-instrumentation-fastapi`, and
outgoing calls by the `httpx` and `requests` instrumentations. Their names
depend on the semantic-convention mode (`OTEL_SEMCONV_STABILITY_OPT_IN`), so
both the default and the stable names are catalogued.

| Metric | Labels kept |
| --- | --- |
| `http.server.duration`, `http.server.request.duration`, `http.server.request.size`, `http.server.request.body.size`, `http.server.response.size`, `http.server.response.body.size` | `http.method`, `http.status_code`, `http.target`, `http.request.method`, `http.response.status_code`, `http.route` |
| `http.server.active_requests` | `http.method`, `http.request.method` |
| `http.client.duration`, `http.client.request.duration` | `http.method`, `http.status_code`, `net.peer.name`, `http.request.method`, `http.response.status_code`, `server.address` |

Notes:

- In the default conventions the FastAPI instrumentation puts the **route
  template** in `http.target` (`/runs/{run_id}`), and omits it for a request
  that matches no route; it never records the raw path or query string. The
  stable conventions use `http.route`. The test drives a real instrumented app
  with an id in the path and checks this.
- Dropped by the Views: `http.server_name` (copied from the client's `Host`
  header, so any caller could create new series), `http.host`,
  `net.host.port`, `http.scheme` and `http.flavor`.
- HTTP methods are normalized by the instrumentation (unknown methods become
  `_OTHER`). Status codes are kept as codes rather than classes because they
  are already a small set and dashboards conventionally group them.
- Client `net.peer.name` / `server.address` is the host of a configured
  service (GitHub, Supabase, Groq); full URLs are never labels.
- The SDK also caps every metric stream at 2 000 attribute sets by default,
  as a last-resort safety net.

## Where the identifiers go instead

| Identifier | Where to find it |
| --- | --- |
| Run id, repository | `agent.run_id` / `agent.repo` span attributes, run logs (`logs` table), Celery task arguments |
| Raw request path, `Host` | Spans from the FastAPI instrumentation |
| Error messages | Logs and span events (`record_exception`) |

## Where the metrics are recorded

- `agent_runs_total` and `agent_run_duration_seconds`: `backend/tasks.py`,
  once per run that held the repository lock, when it ends.
- `llm_tokens_total`, `llm_phase_tokens_total` and
  `llm_estimated_cost_usd_total`: `backend/telemetry_metrics.py`, from the
  engine's `TelemetryEvent`s (phase from the event, kind `total` because the
  engine does not split prompt and completion tokens; the cost of an unsplit
  count is priced at the model's higher rate, an upper bound). The SDK
  package (`backend/automaintainer`) never imports `observability`.
- `celery_worker_heartbeat`: every Celery worker process, through the hooks
  `celery_app.py` installs.

`agent_runs_active`, `llm_request_duration_seconds`,
`github_api_calls_total`, `celery_queue_depth` and
`celery_task_duration_seconds` are catalogued but not recorded by any caller
yet. `observability.py` enables OpenTelemetry only when every module it
imports is installed (the `server` extra); otherwise it runs in no-op mode.
