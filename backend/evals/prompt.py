"""The Implementer prompt, as ``agents.implementer_node`` builds it.

``implementer_node`` builds its prompt inline, interleaved with GitHub calls,
so it cannot be called on a local fixture. This module reproduces exactly the
same strings from the same building blocks (``untrusted_block`` and
``UNTRUSTED_DATA_NOTICE``). ``evals/tests/test_prompt_parity.py`` runs the real
node with a mocked GitHub client and fails if the two ever diverge, so a prompt
change in agents.py must be mirrored here (and changes ``PROMPT_VERSION``).
"""

from __future__ import annotations

import hashlib

from agents import UNTRUSTED_DATA_NOTICE, untrusted_block

NEW_FILE_PLACEHOLDER = "(This file does not exist yet; create it from the task.)"


def build_implementer_prompt(
    task_text: str,
    target_file: str,
    original_content: str,
    *,
    file_exists: bool = True,
    review: str = "",
) -> tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for one Implementer call."""
    action = (
        "Modify the supplied repository file"
        if file_exists
        else "Create the referenced repository file"
    )
    system_prompt = (
        "You are the Implementer Agent. "
        f"{action} "
        "to satisfy the task and Maintainer feedback. "
        "Preserve unrelated code and formatting. Output ONLY the complete file contents, "
        "without Markdown fences and without the <untrusted-data> markers. "
        + UNTRUSTED_DATA_NOTICE
    )
    user_prompt = (
        f"Task:\n{untrusted_block('task', task_text)}\n\n"
        f"Repository file: {target_file}\n\n"
        f"Current file contents:\n{untrusted_block(target_file, original_content)}\n\n"
        "Maintainer feedback:\n"
        + (
            untrusted_block("maintainer review", review)
            if review
            else "No feedback; implement the task."
        )
    )
    return system_prompt, user_prompt


def prompt_sha256(system_prompt: str, user_prompt: str) -> str:
    """Hash of one rendered prompt (keys cassettes to their exact input)."""
    return hashlib.sha256(f"{system_prompt}\0{user_prompt}".encode()).hexdigest()


def _prompt_version() -> str:
    # Rendering with placeholders hashes the template, not any task's data.
    template = build_implementer_prompt("{task}", "{path}", "{content}")
    return prompt_sha256(*template)[:12]


PROMPT_VERSION = _prompt_version()
