"""Greedy word wrapping for plain-text emails."""


def wrap(text: str, width: int) -> list[str]:
    """Split ``text`` into lines of at most ``width`` characters."""
    lines = []
    current = ""
    for word in text.split(" "):
        if len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        lines.append(current)
    return lines
