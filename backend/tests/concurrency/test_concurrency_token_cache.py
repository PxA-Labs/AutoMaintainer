"""Installation token cache under thread contention (github_app).

The cache is keyed by (installation, repository, permissions, App id) and
guarded by one lock held only around dict reads and writes, never around the
mint. Its documented contract (get_installation_token_sync): "Two callers
missing the cache at once may both mint a token; both are valid and the
later one simply replaces the earlier." So these tests do not demand a
single mint per key. They require:

- no thread ever gets another App's token, with two Apps sharing one
  installation id, whether the App comes from the credential or from the
  engine context the thread runs in;
- mints are bounded by the callers that raced on a cold or expired entry,
  and a warm entry is served without a mint;
- an expired entry is refreshed for its own App only;
- invalidation racing with fetches never raises and leaves no stale entry.
"""

import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import github_app
from automaintainer.context import current_engine
from automaintainer.engine import AutoMaintainerEngine

from concurrency_world import make_config

INSTALLATION = 5
APPS = ("101", "202")
THREADS = 16
CALLS_PER_THREAD = 40


def _identity(app_id):
    return github_app.GitHubAppIdentity(app_id=app_id, private_key=f"pem-{app_id}")


def _hammer(work, threads=THREADS):
    """Run ``work(i)`` in ``threads`` threads released together."""
    start = threading.Barrier(threads)

    def worker(i):
        start.wait(30)
        return work(i)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        return list(pool.map(worker, range(threads)))


def _slow_mint(monkeypatch, mint_log, *, lifetime=3600.0, delay=0.005):
    """A mint that takes a while, so concurrent misses overlap."""

    def mint(installation_id, repository, required, optional, app=None):
        app_id = (app or github_app.current_app_identity()).app_id
        mint_log.add(app_id, installation_id)
        time.sleep(delay)
        return f"tok-{app_id}-{installation_id}", time.time() + lifetime

    monkeypatch.setattr(github_app, "_mint_installation_token", mint)


def test_two_apps_same_installation_never_cross(monkeypatch, mint_log):
    _slow_mint(monkeypatch, mint_log)

    def work(i):
        app_id = APPS[i % 2]
        creds = github_app.GitHubCredentials(
            installation_id=INSTALLATION, repository="o/r", app=_identity(app_id)
        )
        return app_id, {creds.token() for _ in range(CALLS_PER_THREAD)}

    results = _hammer(work)

    for app_id, tokens in results:
        assert tokens == {f"tok-{app_id}-{INSTALLATION}"}
    for app_id in APPS:
        # At most one mint per thread that found the entry cold.
        assert 1 <= mint_log.count(app_id, INSTALLATION) <= THREADS // 2
    cache = github_app._installation_token_cache
    assert sorted(k[3] for k in cache) == sorted(APPS)
    for key, (token, _) in cache.items():
        assert token == f"tok-{key[3]}-{INSTALLATION}"

    # Warm: no thread mints again.
    minted = len(mint_log.calls)
    _hammer(work)
    assert len(mint_log.calls) == minted


def test_engine_context_selects_the_app_in_worker_threads(
    tmp_path, monkeypatch, mint_log
):
    """Threads running in two engines' contexts (as asyncio.to_thread
    workers of their runs do) and a credential handed to a thread of the
    other engine: the token always follows the credential's App."""
    _slow_mint(monkeypatch, mint_log)
    contexts = [
        AutoMaintainerEngine(
            make_config(
                tmp_path,
                f"e{app_id}",
                github_app_id=app_id,
                github_private_key="pem",
                github_auth_mode="app",
            )
        )._context
        for app_id in APPS
    ]

    def in_engine(ctx, fn):
        def bound():
            current_engine.set(ctx)
            return fn()

        return contextvars.copy_context().run(bound)

    def credentials():
        return github_app.credentials_for_installation(
            INSTALLATION, purpose="test", repository="o/r"
        )

    def work(i):
        mine, other = contexts[i % 2], contexts[(i + 1) % 2]
        creds = in_engine(mine, credentials)
        own = {in_engine(mine, creds.token) for _ in range(CALLS_PER_THREAD // 2)}
        # The same credential used inside the other engine's context.
        foreign = {in_engine(other, creds.token) for _ in range(CALLS_PER_THREAD // 2)}
        return APPS[i % 2], own | foreign | {creds.token()}

    for app_id, tokens in _hammer(work):
        assert tokens == {f"tok-{app_id}-{INSTALLATION}"}
    assert sorted(k[3] for k in github_app._installation_token_cache) == sorted(APPS)


def test_expired_entries_are_refreshed_per_app(monkeypatch, mint_log):
    """A token inside the refresh margin is re-minted, for its own App, and
    never served from the other App's fresh entry."""
    margin = github_app.TOKEN_REFRESH_MARGIN_SECONDS
    lifetimes = {"101": margin - 1, "202": 3600.0}  # 101 is always stale

    def mint(installation_id, repository, required, optional, app=None):
        app_id = app.app_id
        mint_log.add(app_id, installation_id)
        return f"tok-{app_id}-{len(mint_log.calls)}", time.time() + lifetimes[app_id]

    monkeypatch.setattr(github_app, "_mint_installation_token", mint)

    def work(i):
        app_id = APPS[i % 2]
        creds = github_app.GitHubCredentials(
            installation_id=INSTALLATION, app=_identity(app_id)
        )
        return app_id, [creds.token() for _ in range(10)]

    results = _hammer(work, threads=8)

    for app_id, tokens in results:
        assert all(t.startswith(f"tok-{app_id}-") for t in tokens)
    # The stale App mints on every call; the fresh one once per cold racer.
    assert mint_log.count("101", INSTALLATION) == 4 * 10
    assert 1 <= mint_log.count("202", INSTALLATION) <= 4


def test_invalidation_racing_fetches(monkeypatch, mint_log):
    _slow_mint(monkeypatch, mint_log, delay=0.001)
    stop = threading.Event()
    errors = []

    def invalidate():
        while not stop.is_set():
            try:
                github_app.invalidate_installation_tokens(INSTALLATION)
            except Exception as e:  # pragma: no cover - the failure mode
                errors.append(e)

    invalidator = threading.Thread(target=invalidate)
    invalidator.start()
    try:

        def work(i):
            app_id = APPS[i % 2]
            creds = github_app.GitHubCredentials(
                installation_id=INSTALLATION, app=_identity(app_id)
            )
            return app_id, {creds.token() for _ in range(CALLS_PER_THREAD)}

        results = _hammer(work, threads=8)
    finally:
        stop.set()
        invalidator.join(10)

    assert errors == []
    for app_id, tokens in results:
        assert tokens == {f"tok-{app_id}-{INSTALLATION}"}
    github_app.invalidate_installation_tokens(INSTALLATION)
    assert github_app._installation_token_cache == {}
