"""OpenTelemetry must actually load in a lock-file install.

observability.py imports the whole OpenTelemetry stack inside one try block
and sets OPENTELEMETRY_AVAILABLE = False on any ImportError. An import of a
package that is not in uv.lock (opentelemetry-instrumentation-logging was one)
silently disabled tracing and every metric. When the SDK is installed, every
import in that block must succeed.
"""

import importlib.util

import pytest

import observability


@pytest.mark.skipif(
    importlib.util.find_spec("opentelemetry.sdk") is None,
    reason="OpenTelemetry SDK not installed in this environment",
)
def test_opentelemetry_loads_when_the_sdk_is_installed():
    assert observability.OPENTELEMETRY_AVAILABLE is True
