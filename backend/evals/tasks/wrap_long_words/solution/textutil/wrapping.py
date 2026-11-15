"""Greedy word wrapping for plain-text emails."""


def wrap(text: str, width: int) -> list[str]:
    """Split ``text`` into lines of at most ``width`` characters."""
    if width < 1:
        raise ValueError("width must be at least 1")
    lines = []
    current = ""
    for word in text.split():
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
