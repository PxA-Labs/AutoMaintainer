"""The e2e harness is offline except for the model: a whole engine run (and a
Reviewer case) makes no network connection beyond loopback.

The guard covers this process, where the engine, the fakes and the
scripted model run. Verification and hidden tests run pytest in child
processes, which it does not see; their code is the task's own.
"""

import asyncio
import ipaddress
import socket
import sys

import pytest

from evals.e2e.policies import ScriptedTransport, oracle_pipeline, oracle_reviewer
from evals.e2e.runner import run_pipeline_task, run_review_case
from evals.e2e.taskset import load_e2e_tasks, load_review_cases

ITEM = load_e2e_tasks(["inflection_ordinal_teens"])[0]
[CASE] = load_review_cases(["bad_ordinal_partial_teens"])


def _loopback(host) -> bool:
    if host in (None, "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture
def no_network(monkeypatch):
    """Records and refuses every non-loopback lookup or connection."""
    attempts = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def refuse(what, host):
        attempts.append((what, host))
        raise OSError(f"network access in an offline eval: {what} {host!r}")

    def connect(sock, address):
        host = address[0] if isinstance(address, tuple) else None
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(host):
            refuse("connect", host)
        return real_connect(sock, address)

    def connect_ex(sock, address):
        host = address[0] if isinstance(address, tuple) else None
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(host):
            refuse("connect_ex", host)
        return real_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if not _loopback(host):
            refuse("getaddrinfo", host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setenv("VERIFY_PYTHON", sys.executable)
    return attempts


def test_the_guard_refuses_an_external_connection(no_network):
    with pytest.raises(OSError, match="offline eval"):
        socket.create_connection(("api.groq.com", 443), timeout=1)
    assert no_network and no_network[0][1] == "api.groq.com"


def test_an_oracle_engine_run_makes_no_external_connection(no_network, tmp_path):
    record = asyncio.run(
        run_pipeline_task(
            ITEM,
            transport=ScriptedTransport(oracle_pipeline(ITEM)),
            root=tmp_path,
        )
    )
    assert record["error"] is None and record["resolved"] is True
    assert no_network == []


def test_a_reviewer_case_makes_no_external_connection(no_network, tmp_path):
    record = asyncio.run(
        run_review_case(
            ITEM,
            CASE,
            transport=ScriptedTransport(oracle_reviewer(CASE)),
            root=tmp_path,
        )
    )
    assert record["error"] is None and record["verdict"] == "reject"
    assert no_network == []
