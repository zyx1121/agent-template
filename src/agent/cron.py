"""5-field cron expression matching — a minimal, dependency-free subset of standard cron
syntax (`minute hour dom month dow`), used by the JobQueue tick in handlers.py to decide
whether a persisted schedule should fire for a given (whole) minute. No timezone handling:
matches against whatever naive `dt` the caller passes in — the bot process's local clock.

Syntax per field: `*` (any), a number, a `,`-separated list, a `a-b` range, a `*/n` or
`a-b/n` step. dow accepts 0-7 where both 0 and 7 mean Sunday (cron convention). Standard cron
semantics for the dom/dow interaction: when BOTH are restricted (neither is `*`), a match on
EITHER is enough — e.g. "1st of the month OR every Monday", not an AND of the two.
"""
from __future__ import annotations

from datetime import datetime

_FIELD_NAMES = ("minute", "hour", "dom", "month", "dow")
_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _parse_item(item: str, lo: int, hi: int) -> set[int]:
    """One comma-separated item: `*`, `*/n`, `a`, `a-b`, or `a-b/n`."""
    step = 1
    if "/" in item:
        item, step_s = item.split("/", 1)
        try:
            step = int(step_s)
        except ValueError:
            raise ValueError(f"bad step {step_s!r} in {item}/{step_s}") from None
        if step <= 0:
            raise ValueError(f"step must be positive: {item}/{step_s}")
    if item == "*":
        start, end = lo, hi
    elif "-" in item:
        a, b = item.split("-", 1)
        try:
            start, end = int(a), int(b)
        except ValueError:
            raise ValueError(f"bad range {item!r}") from None
        if start > end:
            raise ValueError(f"range start > end: {item!r}")
    else:
        try:
            start = end = int(item)
        except ValueError:
            raise ValueError(f"not a number, range, or *: {item!r}") from None
    if not (lo <= start <= hi and lo <= end <= hi):
        raise ValueError(f"value out of range [{lo},{hi}]: {item!r}")
    return set(range(start, end + 1, step))


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    if not field:
        raise ValueError("empty field")
    values: set[int] = set()
    for item in field.split(","):
        values |= _parse_item(item, lo, hi)
    return values


def _parse(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    """Returns (minutes, hours, doms, months, dows, dom_is_star, dow_is_star). Raises
    ValueError with a field-labeled message on malformed input."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(
            f"cron expression needs 5 fields (minute hour dom month dow), got {len(parts)}: {expr!r}"
        )
    parsed = []
    for name, part, (lo, hi) in zip(_FIELD_NAMES, parts, _FIELD_BOUNDS):
        try:
            parsed.append(_parse_field(part, lo, hi))
        except ValueError as e:
            raise ValueError(f"{name} field {part!r}: {e}") from e
    minutes, hours, doms, months, dows = parsed
    dows = {0 if v == 7 else v for v in dows}  # 7 == Sunday, same as 0
    return minutes, hours, doms, months, dows, parts[2] == "*", parts[4] == "*"


def cron_matches(expr: str, dt: datetime) -> bool:
    """True if `dt` (evaluated at minute resolution) falls on this cron schedule. Raises
    ValueError if `expr` is malformed — call `validate_cron()` first if the expression came
    from user input rather than an already-stored schedule."""
    minutes, hours, doms, months, dows, dom_star, dow_star = _parse(expr)
    if dt.minute not in minutes or dt.hour not in hours or dt.month not in months:
        return False
    dow = dt.isoweekday() % 7  # Mon=1..Sun=7 -> cron Sun=0,Mon=1..Sat=6
    if dom_star and dow_star:
        return True
    if dom_star:
        return dow in dows
    if dow_star:
        return dt.day in doms
    return dt.day in doms or dow in dows  # dom/dow both restricted -> OR, standard cron semantics


def validate_cron(expr: str) -> str | None:
    """Returns an error message if `expr` isn't a valid 5-field cron expression, else None.
    Callers that persist a cron string (schedule_add/schedule_edit in mcp_schedule.py) must
    call this before storing it — cron_matches() itself only raises, it doesn't validate."""
    try:
        _parse(expr)
    except ValueError as e:
        return str(e)
    return None
