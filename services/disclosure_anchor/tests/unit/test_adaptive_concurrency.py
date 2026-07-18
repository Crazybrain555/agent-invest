"""AIMD adaptive parse concurrency (Netflix concurrency-limits pattern)."""

from __future__ import annotations

import unittest

from disclosure_anchor.application.worker.concurrency import (
    AdaptiveConcurrencyLimit,
)


class AdaptiveConcurrencyLimitTests(unittest.TestCase):
    def test_starts_at_bound_and_backs_off_multiplicatively(self) -> None:
        limit = AdaptiveConcurrencyLimit(max_limit=16)
        self.assertEqual(limit.current, 16)
        limit.on_drop()
        self.assertEqual(limit.current, 8)
        limit.on_drop()
        limit.on_drop()
        limit.on_drop()
        limit.on_drop()
        self.assertEqual(limit.current, 1)
        limit.on_drop()
        self.assertEqual(limit.current, 1)

    def test_success_grows_additively_up_to_bound(self) -> None:
        limit = AdaptiveConcurrencyLimit(max_limit=16)
        limit.on_drop()
        for _ in range(20):
            limit.on_success(inflight=8)
        self.assertEqual(limit.current, 16)

    def test_underutilized_successes_never_grow_the_limit(self) -> None:
        limit = AdaptiveConcurrencyLimit(max_limit=16)
        limit.on_drop()
        limit.on_drop()
        for _ in range(20):
            limit.on_success(inflight=1)
        self.assertEqual(limit.current, 4)


if __name__ == "__main__":
    unittest.main()
