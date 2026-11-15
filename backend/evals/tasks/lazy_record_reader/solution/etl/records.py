"""Parsing of exported payment records."""


def read_records(lines):
    """Lazily parse ``name,amount`` lines into ``(name, amount)`` tuples."""
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) != 2:
            raise ValueError(f"line {number}: expected 'name,amount'")
        name, amount = parts[0].strip(), parts[1].strip()
        try:
            value = int(amount)
        except ValueError:
            raise ValueError(f"line {number}: bad amount {amount!r}") from None
        yield name, value
