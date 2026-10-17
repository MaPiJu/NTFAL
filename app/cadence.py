"""When a horizon is recomputed: right after its bars close.

Hyperliquid's candles are aligned to the epoch (UTC; its 1w candles open on
Thursday, the epoch's weekday), so the closes of an `interval_s` bar are the
multiples of `interval_s`. A refresh scheduled at "every N seconds from now"
reads its bars at a random point of their life: up to a whole bar late. Elder
does his homework after the close; a refresh here runs `BAR_CLOSE_DELAY_SECONDS`
after it, once the bar is final.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

# How long after a bar closes its refresh runs, so the closed bar is final.
BAR_CLOSE_DELAY_SECONDS = 60


def first_run(
    interval_s: int, now: float | None = None, delay_s: int = BAR_CLOSE_DELAY_SECONDS
) -> datetime:
    """The first moment after `now` that lies `delay_s` past a bar close: a job
    started at it with an `interval_s` period always runs just after a close."""
    now = time.time() if now is None else now
    closes = math.floor((now - delay_s) / interval_s) + 1
    return datetime.fromtimestamp(closes * interval_s + delay_s, tz=UTC)
