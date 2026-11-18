"""Price table for the cost metric (USD per million tokens).

The defaults are Groq's earlier public on-demand list prices for the two
models the engine routes to. They could NOT be confirmed when this table was
written (2026-11-18): https://console.groq.com/docs/models showed "Contact
sales" for both models. Treat every cost figure computed from them as an
estimate, and pass the current contract prices with ``--prices FILE`` (a
JSON object of the same shape) before quoting a cost. The report records
which table was used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

DEFAULT_PRICES = {
    "llama-3.3-70b-versatile": {"input_per_mtok": 0.59, "output_per_mtok": 0.79},
    "llama-3.1-8b-instant": {"input_per_mtok": 0.05, "output_per_mtok": 0.08},
}
DEFAULT_SOURCE = (
    "built-in table: Groq's earlier public list prices, not confirmed on "
    "2026-11-18 (the model page shows 'Contact sales'); verify before quoting"
)


def load_prices(path: Optional[Path]) -> tuple[dict, str]:
    """(price table, description of its source)."""
    if path is None:
        return DEFAULT_PRICES, DEFAULT_SOURCE
    table = json.loads(Path(path).read_text(encoding="utf-8"))
    for model, entry in table.items():
        for key in ("input_per_mtok", "output_per_mtok"):
            if not isinstance(entry.get(key), (int, float)) or entry[key] < 0:
                raise ValueError(f"{path}: {model}.{key} must be a number >= 0")
    return table, f"file {Path(path).name}"


def cost_usd(
    model: Optional[str],
    prompt_tokens: Optional[int],
    completion_tokens: Optional[int],
    prices: dict = DEFAULT_PRICES,
) -> Optional[float]:
    """Cost of one call or run, or None when the model or usage is unknown."""
    entry = prices.get(model or "")
    if entry is None or prompt_tokens is None or completion_tokens is None:
        return None
    return (
        prompt_tokens * entry["input_per_mtok"]
        + completion_tokens * entry["output_per_mtok"]
    ) / 1_000_000
