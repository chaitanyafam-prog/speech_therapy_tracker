"""Tiny in-memory sliding-window limiter. It protects the paid Google quota from being drained by
repeated searches. Correct for one server process; it resets on restart."""
import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self):
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key, limit, window, now=time.monotonic):
        """Record an event; return 0 if allowed, else the seconds to wait."""
        t = now()
        with self._lock:
            dq = self._events[key]
            while dq and t - dq[0] > window:
                dq.popleft()
            if len(dq) >= limit:
                return max(1, int(window - (t - dq[0])) + 1)
            dq.append(t)
            if len(self._events) > 5000:                    # drop idle keys so memory stays bounded
                for k in [k for k, v in self._events.items() if not v or t - v[-1] > window]:
                    del self._events[k]
            return 0
