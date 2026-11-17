"""Load Secret Manager secrets into the environment, then exec a command.

Runs inside the AutoMaintainer worker container on the Compute Engine VM
(standard library only, so it works with the app image's Python):

    python secrets_exec.py celery -A celery_app worker ...

AUTOMAINTAINER_SECRETS lists ``ENV_NAME=projects/<p>/secrets/<id>`` pairs,
comma-separated. For each, the latest version is read with the VM service
account's access token from the metadata server and stored in ENV_NAME.
Values never reach the VM's disk or the ``docker run`` command line, and
multi-line values (a PEM key) are kept intact, which ``--env-file`` cannot
do. Any failure exits 1 before the command starts (systemd retries), naming
the variable but never printing a value.
"""

import base64
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SPEC_ENV = "AUTOMAINTAINER_SECRETS"
TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
ACCESS_URL = "https://secretmanager.googleapis.com/v1/{name}/versions/latest:access"
TIMEOUT_SECONDS = 10

_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
_SECRET_NAME = re.compile(r"projects/[A-Za-z0-9-]+/secrets/[A-Za-z0-9_-]+")


def _fail(message: str) -> None:
    print(f"secrets_exec: {message}", file=sys.stderr)
    sys.exit(1)


def parse_spec(spec: str) -> list[tuple[str, str]]:
    pairs = []
    for item in filter(None, (part.strip() for part in spec.split(","))):
        env_name, sep, secret = item.partition("=")
        if not sep or not _ENV_NAME.fullmatch(env_name):
            _fail(f"malformed entry in {SPEC_ENV}: {env_name!r}")
        if not _SECRET_NAME.fullmatch(secret):
            _fail(f"{env_name}: not a secret name (projects/<p>/secrets/<id>)")
        pairs.append((env_name, secret))
    return pairs


def _get_json(url: str, headers: dict) -> dict:
    with urlopen(Request(url, headers=headers), timeout=TIMEOUT_SECONDS) as resp:
        return json.load(resp)


def _access_token() -> str:
    return _get_json(TOKEN_URL, {"Metadata-Flavor": "Google"})["access_token"]


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        print("usage: secrets_exec.py COMMAND [ARG...]", file=sys.stderr)
        sys.exit(2)
    pairs = parse_spec(os.environ.pop(SPEC_ENV, ""))
    if pairs:
        try:
            token = _access_token()
        except (HTTPError, URLError, OSError, KeyError, ValueError) as e:
            _fail(f"cannot get an access token from the metadata server: {e}")
        auth = {"Authorization": f"Bearer {token}"}
        for env_name, secret in pairs:
            try:
                body = _get_json(ACCESS_URL.format(name=secret), auth)
                value = base64.b64decode(body["payload"]["data"]).decode("utf-8")
            except HTTPError as e:
                _fail(f"{env_name}: Secret Manager answered HTTP {e.code}")
            except (URLError, OSError, KeyError, ValueError) as e:
                _fail(f"{env_name}: cannot read the secret ({type(e).__name__})")
            os.environ[env_name] = value
    os.execvp(argv[1], argv[1:])


if __name__ == "__main__":
    main(sys.argv)
