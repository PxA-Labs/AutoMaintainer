"""Static checks of the Cloud Monitoring alerts and dashboard (monitoring.tf).

The alert policies and the dashboard filter on metric types that the
backend writes through OTEL_CLOUD_MONITORING_ENABLED
(``workload.googleapis.com/<metric name>``). A renamed metric or label
would leave an alert that never fires, which ``terraform validate`` cannot
see, so these tests tie every filter to observability's metric catalogue.
They parse text only: no Terraform, no Google credentials, no network.
"""

import re
from pathlib import Path

import pytest

import observability

REPO_ROOT = Path(__file__).resolve().parents[2]
TF = REPO_ROOT / "infra" / "terraform"
MONITORING_TF = TF / "monitoring.tf"
PREFIX = observability.CLOUD_MONITORING_METRIC_PREFIX

ALERTS = {
    "run_failure_rate": "agent_runs_total",
    "run_duration_p95": "agent_run_duration_seconds",
    "llm_cost_per_day": "llm_estimated_cost_usd_total",
    "worker_heartbeat_absent": "celery_worker_heartbeat",
    "api_5xx_ratio": None,  # a built-in Cloud Run metric
}


def _strip_comments(hcl: str) -> str:
    return "\n".join(re.sub(r"\s+#.*$|^\s*#.*$", "", line) for line in hcl.splitlines())


def _block(text: str, header: str) -> str:
    """The body of the first block whose header matches ``header``."""
    m = re.search(header + r"\s*\{", text)
    assert m, header
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i - 1]


@pytest.fixture(scope="module")
def monitoring() -> str:
    assert MONITORING_TF.is_file(), MONITORING_TF
    return _strip_comments(MONITORING_TF.read_text(encoding="utf-8"))


def _alert(monitoring: str, name: str) -> str:
    return _block(
        monitoring, rf'resource\s+"google_monitoring_alert_policy"\s+"{name}"'
    )


def _metric_types(text: str) -> list[str]:
    return re.findall(r'metric\.type\s*=\s*\\"([^"\\]+)\\"', text)


def _label_filters(text: str) -> list[tuple[str, str]]:
    """(label, value) for every ``metric.label.X="v"`` and, one pair per
    alternative, every ``metric.label.X=monitoring.regex.full_match("a|b")``."""
    pairs = re.findall(r'metric\.label\.([a-z_]+)\s*=\s*\\"([^"\\]+)\\"', text)
    for label, pattern in re.findall(
        r'metric\.label\.([a-z_]+)\s*=\s*monitoring\.regex\.full_match\(\\"([^"\\]+)\\"\)',
        text,
    ):
        pairs.extend((label, value) for value in pattern.split("|"))
    return pairs


# --- APIs and IAM -------------------------------------------------------------


def test_monitoring_api_is_enabled_and_alerts_wait_for_it(monitoring):
    main = (TF / "main.tf").read_text(encoding="utf-8")
    assert '"monitoring.googleapis.com"' in main
    for name in ALERTS:
        assert "google_project_service.services" in _alert(monitoring, name), name


def test_api_and_worker_service_accounts_can_write_metrics(monitoring):
    grant = _block(
        monitoring, r'resource\s+"google_project_iam_member"\s+"api_metric_writer"'
    )
    assert '"roles/monitoring.metricWriter"' in grant
    assert "google_service_account.api.member" in grant
    # The worker already holds the role in iam.tf. A second resource for the
    # same binding would remove it when either one is destroyed.
    iam = (TF / "iam.tf").read_text(encoding="utf-8")
    worker = _block(iam, r'resource\s+"google_project_iam_member"\s+"worker"')
    assert '"roles/monitoring.metricWriter"' in worker
    assert "google_service_account.worker.member" not in monitoring


# --- Notification channel ---------------------------------------------------------


def test_alert_email_has_no_default_and_null_means_no_channel(monitoring):
    var = _block(monitoring, r'variable\s+"monitoring_alert_email"')
    assert not re.search(r"^\s*default\s*=", var, re.M)
    assert re.search(r"nullable\s*=\s*true", var)
    channel = _block(
        monitoring,
        r'resource\s+"google_monitoring_notification_channel"\s+"alert_email"',
    )
    assert re.search(
        r"count\s*=\s*var\.monitoring_alert_email\s*==\s*null\s*\?\s*0\s*:\s*1", channel
    )
    assert 'type         = "email"' in channel or re.search(
        r'type\s*=\s*"email"', channel
    )
    assert "email_address = var.monitoring_alert_email" in channel
    for name in ALERTS:
        assert re.search(
            r"notification_channels\s*=\s*google_monitoring_notification_channel\.alert_email\[\*\]\.id",
            _alert(monitoring, name),
        ), name


# --- Alert policies use the metrics the backend emits --------------------------


def test_every_workload_metric_in_monitoring_tf_is_emitted(monitoring):
    catalogue = observability.all_metric_labels()
    workload = [t for t in _metric_types(monitoring) if t.startswith(PREFIX + "/")]
    assert workload
    for metric_type in workload:
        assert metric_type[len(PREFIX) + 1 :] in catalogue, metric_type


@pytest.mark.parametrize("name,metric", [(n, m) for n, m in ALERTS.items() if m])
def test_each_app_alert_filters_on_its_emitted_metric(monitoring, name, metric):
    body = _alert(monitoring, name)
    types = set(_metric_types(body))
    assert types == {f"{PREFIX}/{metric}"}, types
    allowed = observability.all_metric_labels()[metric]
    for label, value in _label_filters(body):
        assert label in allowed, (name, label)
        if label == "outcome":
            assert value in observability.RUN_OUTCOMES, value
    # Metrics written by the exporter carry the generic_task resource; some
    # conditions are refused without a resource.type restriction.
    assert 'resource.type=\\"generic_task\\"' in body


def test_app_alerts_wait_until_the_metrics_exist(monitoring):
    # Cloud Monitoring refuses a policy on a workload metric type that has
    # never been written (404), so these are created only when switched on.
    var = _block(monitoring, r'variable\s+"monitoring_app_alerts_enabled"')
    assert re.search(r"default\s*=\s*false", var)
    for name, metric in ALERTS.items():
        body = _alert(monitoring, name)
        gated = re.search(
            r"count\s*=\s*var\.monitoring_app_alerts_enabled\s*\?\s*1\s*:\s*0", body
        )
        assert bool(gated) == bool(metric), name


def test_failure_rate_counts_failed_and_timed_out_runs_over_all_runs(monitoring):
    body = _alert(monitoring, "run_failure_rate")
    numerator = re.search(r'\n\s*filter\s*=\s*"([^\n]+)"', body)
    assert numerator
    outcomes = {v for k, v in _label_filters(numerator.group(1)) if k == "outcome"}
    # A run stopped by the 30-minute limit is recorded as "timeout" (tasks.py)
    # and must count as a failure, not vanish from the numerator.
    assert outcomes == {"failed", "timeout"}
    assert outcomes <= observability.RUN_OUTCOMES
    denominator = re.search(r'denominator_filter\s*=\s*"([^\n]+)"', body)
    assert denominator and "outcome" not in denominator.group(1)
    assert "var.alert_run_failure_rate" in body


def test_duration_alert_is_a_p95(monitoring):
    body = _alert(monitoring, "run_duration_p95")
    assert "REDUCE_PERCENTILE_95" in body
    assert "ALIGN_DELTA" in body
    assert "var.alert_run_duration_p95_seconds" in body


def test_cost_alert_sums_a_day_of_estimated_cost(monitoring):
    body = _alert(monitoring, "llm_cost_per_day")
    assert re.search(r'alignment_period\s*=\s*"86400s"', body)
    assert "REDUCE_SUM" in body
    assert "var.alert_llm_cost_per_day_usd" in body
    assert "estimate" in body.lower()


def test_heartbeat_alert_fires_when_the_metric_is_absent(monitoring):
    body = _alert(monitoring, "worker_heartbeat_absent")
    assert "condition_absent" in body
    assert "condition_threshold" not in body
    assert "var.alert_worker_heartbeat_absent_seconds" in body


def test_api_5xx_ratio_uses_cloud_run_request_count(monitoring):
    body = _alert(monitoring, "api_5xx_ratio")
    assert set(_metric_types(body)) == {"run.googleapis.com/request_count"}
    assert 'metric.label.response_code_class=\\"5xx\\"' in body
    assert 'resource.type=\\"cloud_run_revision\\"' in body
    assert "google_cloud_run_v2_service.api.name" in body
    denominator = re.search(r'denominator_filter\s*=\s*"([^\n]+)"', body)
    assert denominator and "response_code_class" not in denominator.group(1)
    assert "var.alert_api_5xx_ratio" in body


# --- Dashboard -------------------------------------------------------------------


def test_dashboard_charts_every_alerted_metric(monitoring):
    dashboard = _block(
        monitoring, r'resource\s+"google_monitoring_dashboard"\s+"automaintainer"'
    )
    assert "jsonencode(" in dashboard
    assert "local.monitoring_charts" in dashboard
    charts = monitoring.split("monitoring_charts = [", 1)[1].split("\n  ]\n", 1)[0]
    charted = set(_metric_types(charts))
    for metric in filter(None, ALERTS.values()):
        assert f"{PREFIX}/{metric}" in charted, metric
    assert "run.googleapis.com/request_count" in charted
    assert f"{PREFIX}/llm_phase_tokens_total" in charted


def test_monitoring_tf_only_adds_resources(monitoring):
    # Shared files stay untouched: the switch reaches the services through
    # var.app_env (main.tf merges it into the API and worker environment).
    assert "OTEL_CLOUD_MONITORING_ENABLED" not in (TF / "run.tf").read_text(
        encoding="utf-8"
    )
    assert not re.search(r'resource\s+"google_project_service"', monitoring)


# --- Docs ------------------------------------------------------------------------


def test_metrics_and_alerts_are_documented():
    deploy = (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    option_d = deploy.split("## Option D", 1)[1].split("\n## ", 1)[0]
    section = option_d.split("### Metrics and alerts", 1)[1].split("\n### ", 1)[0]
    for needle in (
        "OTEL_CLOUD_MONITORING_ENABLED",
        "monitoring_alert_email",
        "monitoring_app_alerts_enabled",
        "LLM_PRICES_JSON",
        "estimate",
        "install_celery_worker_hooks",
    ):
        assert needle in section, needle
    readme = (TF / "README.md").read_text(encoding="utf-8")
    assert "monitoring.tf" in readme
