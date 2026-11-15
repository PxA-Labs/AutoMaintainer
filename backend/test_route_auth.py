"""Whole-surface authentication contract.

This is the regression guard for the class of defect where a new endpoint ships
without an auth dependency. It inspects each route's resolved dependency tree
rather than probing over HTTP, so it cannot be fooled by a handler that happens
to fail for an unrelated reason (a missing database, say).
"""

from fastapi.routing import APIRoute

from main import app, get_current_user

# Routes that are deliberately reachable without credentials. Liveness and
# readiness probes are called by Render/Docker before any user exists.
PUBLIC_PATHS = {
    "/healthz",
    "/healthz/supabase",
    "/healthz/observability",
    # GitHub cannot present a Supabase token. This route is authenticated by
    # HMAC instead: it rejects the request when GITHUB_WEBHOOK_SECRET is unset
    # and verifies X-Hub-Signature-256 over the raw body with compare_digest.
    "/webhooks/github",
    # FastAPI's generated documentation.
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}


def _depends_on(dependant, target) -> bool:
    """True if `target` appears anywhere in this route's dependency tree."""
    if dependant.call is target:
        return True
    return any(_depends_on(sub, target) for sub in dependant.dependencies)


def _api_routes():
    return [route for route in app.routes if isinstance(route, APIRoute)]


def test_every_api_route_requires_authentication():
    unprotected = [
        f"{','.join(sorted(route.methods))} {route.path}"
        for route in _api_routes()
        if route.path not in PUBLIC_PATHS
        and not _depends_on(route.dependant, get_current_user)
    ]

    assert not unprotected, (
        "These routes resolve without authentication:\n  "
        + "\n  ".join(sorted(unprotected))
        + "\n\nAdd Depends(get_org_id)/Depends(require_repo_access), or add the "
        "path to PUBLIC_PATHS with a comment justifying why it is public."
    )


def test_public_paths_are_all_real_routes():
    """Keeps the allowlist honest: a stale entry could mask a real route."""
    declared = {route.path for route in _api_routes()}
    generated = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

    stale = {p for p in PUBLIC_PATHS - generated if p not in declared}
    assert not stale, f"PUBLIC_PATHS lists paths that no longer exist: {stale}"


def test_health_endpoints_stay_public():
    """Probes must not require a token, or deploys will fail health checks."""
    for route in _api_routes():
        if route.path.startswith("/healthz"):
            assert not _depends_on(route.dependant, get_current_user), (
                f"{route.path} must stay public; Render's healthCheckPath "
                "cannot present credentials."
            )


def test_static_mount_is_registered_last():
    """Mount("/") matches every path, so it must not precede any API route.

    Regression guard for the bug where /admin/metrics returned the SPA's 404
    page instead of JSON because the static export was mounted before the admin
    routes were declared. That condition is always true inside the Docker image,
    where dashboard/out exists.
    """
    from starlette.routing import Mount

    routes = list(app.routes)
    mount_indexes = [i for i, r in enumerate(routes) if isinstance(r, Mount)]

    if not mount_indexes:
        import pytest

        pytest.skip("static export not present in this environment")

    last_api_index = max(i for i, r in enumerate(routes) if isinstance(r, APIRoute))

    assert min(mount_indexes) > last_api_index, (
        "A Mount is registered before the last API route and will shadow it. "
        "Keep the StaticFiles mount at the bottom of main.py."
    )
