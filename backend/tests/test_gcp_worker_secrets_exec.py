"""The worker VM's secret launcher (infra/terraform, #527).

On Compute Engine there is no platform feature that turns Secret Manager
secrets into container environment variables, as Cloud Run does for the API.
``secrets_exec.py`` runs inside the worker container: it reads each secret's
latest version with the VM service account's token from the metadata server,
puts it in the environment and replaces itself with the Celery command. The
values never touch the VM's disk or the ``docker run`` command line.

No test reaches the metadata server or Google: ``urlopen`` is replaced.
"""

import base64
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "terraform" / "modules" / "worker" / "secrets_exec.py"

TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
PEM = "-----BEGIN RSA PRIVATE KEY-----\nAAA\nBBB\n-----END RSA PRIVATE KEY-----\n"


@pytest.fixture
def launcher():
    spec = importlib.util.spec_from_file_location("secrets_exec", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def fake_google(launcher, monkeypatch):
    """Serve a token and the given secret values; record every request."""
    # The launcher writes os.environ; give it a copy so no fake secret leaks
    # into the rest of the test session.
    monkeypatch.setattr(launcher.os, "environ", dict(launcher.os.environ))
    calls = []
    values = {}

    def urlopen(request, timeout=None):
        url = request.full_url
        headers = {k.lower(): v for k, v in request.header_items()}
        calls.append((url, headers, timeout))
        if url == TOKEN_URL:
            assert headers["metadata-flavor"] == "Google"
            return _Response(json.dumps({"access_token": "tok"}).encode())
        prefix = "https://secretmanager.googleapis.com/v1/"
        suffix = "/versions/latest:access"
        assert url.startswith(prefix) and url.endswith(suffix), url
        assert headers["authorization"] == "Bearer tok"
        name = url[len(prefix) : -len(suffix)]
        if name not in values:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        data = base64.b64encode(values[name].encode()).decode()
        return _Response(json.dumps({"payload": {"data": data}}).encode())

    monkeypatch.setattr(launcher, "urlopen", urlopen)
    execs = []
    monkeypatch.setattr(
        launcher.os, "execvp", lambda file, args: execs.append((file, list(args)))
    )
    return calls, values, execs


def test_secrets_become_environment_variables_then_the_command_runs(
    launcher, fake_google, monkeypatch
):
    calls, values, execs = fake_google
    values["projects/p/secrets/groq-api-key"] = "gsk-123"
    values["projects/p/secrets/github-app-private-key"] = PEM
    monkeypatch.setenv(
        "AUTOMAINTAINER_SECRETS",
        "GROQ_API_KEY=projects/p/secrets/groq-api-key,"
        "GITHUB_APP_PRIVATE_KEY=projects/p/secrets/github-app-private-key",
    )
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)

    launcher.main(["secrets_exec.py", "celery", "-A", "celery_app", "worker"])

    assert execs == [("celery", ["celery", "-A", "celery_app", "worker"])]
    assert launcher.os.environ["GROQ_API_KEY"] == "gsk-123"
    # Multi-line values survive, unlike with `docker run --env-file`.
    assert launcher.os.environ["GITHUB_APP_PRIVATE_KEY"] == PEM
    # The list of names is not passed on to the worker.
    assert "AUTOMAINTAINER_SECRETS" not in launcher.os.environ
    # One token for all secrets, and every call has a timeout.
    assert [c[0] for c in calls].count(TOKEN_URL) == 1
    assert all(timeout for _, _, timeout in calls)


def test_a_missing_secret_version_fails_closed_without_leaking_values(
    launcher, fake_google, monkeypatch, capsys
):
    calls, values, execs = fake_google
    values["projects/p/secrets/groq-api-key"] = "gsk-should-not-print"
    monkeypatch.setenv(
        "AUTOMAINTAINER_SECRETS",
        "GROQ_API_KEY=projects/p/secrets/groq-api-key,"
        "SUPABASE_SERVICE_KEY=projects/p/secrets/supabase-service-key",
    )

    with pytest.raises(SystemExit) as exc:
        launcher.main(["secrets_exec.py", "celery"])

    assert exc.value.code == 1
    assert execs == []
    err = capsys.readouterr().err
    assert "SUPABASE_SERVICE_KEY" in err and "404" in err
    assert "gsk-should-not-print" not in err


@pytest.mark.parametrize(
    "spec",
    [
        "GROQ_API_KEY",  # no secret name
        "groq=projects/p/secrets/x",  # not an environment variable name
        "GROQ_API_KEY=projects/p/secrets/x/versions/1",  # not a secret name
        "GROQ_API_KEY=https://evil.example/x",
    ],
)
def test_malformed_specs_are_refused_before_any_request(
    launcher, fake_google, monkeypatch, spec
):
    calls, _, execs = fake_google
    monkeypatch.setenv("AUTOMAINTAINER_SECRETS", spec)
    with pytest.raises(SystemExit) as exc:
        launcher.main(["secrets_exec.py", "celery"])
    assert exc.value.code == 1
    assert calls == [] and execs == []


def test_without_secrets_it_only_runs_the_command(launcher, fake_google, monkeypatch):
    calls, _, execs = fake_google
    monkeypatch.setenv("AUTOMAINTAINER_SECRETS", "")
    launcher.main(["secrets_exec.py", "celery", "beat"])
    assert calls == []
    assert execs == [("celery", ["celery", "beat"])]


def test_a_command_is_required(launcher, fake_google, monkeypatch):
    monkeypatch.setenv("AUTOMAINTAINER_SECRETS", "")
    with pytest.raises(SystemExit) as exc:
        launcher.main(["secrets_exec.py"])
    assert exc.value.code == 2
