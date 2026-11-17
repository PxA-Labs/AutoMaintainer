"""Importing agents performs no network calls and creates no files (#186).

The import runs in a fresh interpreter, from an empty temporary working
directory, with an audit hook (PEP 578) installed before anything is
imported. The hook records and refuses every socket connection and name
lookup and records every file opened for writing and every directory
created. Supabase credentials are set, so an eagerly constructed client
would show up too.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent

PROBE = r"""
import json
import os
import sys

events = []
NETWORK = {
    "socket.connect",
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "socket.gethostbyaddr",
    "socket.sendto",
    "socket.sendmsg",
}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def hook(event, args):
    if event in NETWORK:
        events.append([event, repr(args)])
        raise OSError("network access blocked during import")
    if event in ("os.mkdir", "shutil.copyfile", "os.rename", "os.symlink"):
        events.append([event, repr(args[:2])])
    elif event == "open":
        path, mode, flags = args
        writing = any(c in (mode or "") for c in "wax+") or (
            mode is None and isinstance(flags, int) and flags & WRITE_FLAGS
        )
        # os.devnull is opened for writing by some third-party imports; it
        # is not a file on disk.
        if writing and path != os.devnull:
            events.append([event, repr(path)])


sys.addaudithook(hook)

import agents  # noqa: E402

print(
    json.dumps(
        {
            "events": events,
            "supabase_built": "supabase" in vars(agents),
            "graph_built": "app" in vars(agents),
        }
    )
)
"""


def _import_agents_in_subprocess(tmp_path):
    cwd = tmp_path / "cwd"
    tmp = tmp_path / "tmp"
    cwd.mkdir()
    tmp.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(BACKEND_DIR),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMP": str(tmp),
            "TEMP": str(tmp),
            "TMPDIR": str(tmp),
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "service-role-key",
            "GITHUB_AUTH_MODE": "pat",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-B", "-c", PROBE],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    return report, cwd, tmp


def test_importing_agents_has_no_network_or_file_side_effects(tmp_path):
    report, cwd, tmp = _import_agents_in_subprocess(tmp_path)

    assert report["events"] == []
    assert list(cwd.iterdir()) == []
    assert list(tmp.iterdir()) == []
    # The clients and the graph are built on first use, not on import.
    assert report["supabase_built"] is False
    assert report["graph_built"] is False
