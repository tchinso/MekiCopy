from __future__ import annotations

import threading
import unittest

from mekicopy_tasks import TkTaskRunner


class _FakeRoot:
    def __init__(self) -> None:
        self._next_id = 1
        self.callbacks: dict[str, tuple[int, object]] = {}
        self.cancelled: list[str] = []

    def after(self, delay: int, callback: object) -> str:
        identifier = f"after-{self._next_id}"
        self._next_id += 1
        self.callbacks[identifier] = (delay, callback)
        return identifier

    def after_cancel(self, identifier: str) -> None:
        self.cancelled.append(identifier)
        self.callbacks.pop(identifier, None)

    def fire(self, identifier: str) -> None:
        _delay, callback = self.callbacks.pop(identifier)
        assert callable(callback)
        callback()


class TkTaskRunnerTests(unittest.TestCase):
    def test_idle_poll_is_replaced_when_work_is_submitted(self) -> None:
        root = _FakeRoot()
        runner = TkTaskRunner(root, poll_interval_ms=10, idle_poll_interval_ms=30)

        idle_id, (idle_delay, _idle_callback) = next(iter(root.callbacks.items()))
        self.assertEqual(idle_delay, 30)

        completed = threading.Event()
        delivered: list[str] = []

        def operation() -> str:
            completed.set()
            return "done"

        self.assertTrue(
            runner.submit(
                "work",
                operation,
                on_success=delivered.append,
                on_error=lambda exc: self.fail(str(exc)),
            )
        )
        self.assertIn(idle_id, root.cancelled)
        immediate_id, (immediate_delay, _callback) = next(iter(root.callbacks.items()))
        self.assertEqual(immediate_delay, 0)
        self.assertTrue(completed.wait(timeout=1))

        root.fire(immediate_id)

        self.assertEqual(delivered, ["done"])
        _next_id, (next_delay, _next_callback) = next(iter(root.callbacks.items()))
        self.assertEqual(next_delay, 30)
        runner.close()

    def test_running_task_uses_the_shorter_busy_poll(self) -> None:
        root = _FakeRoot()
        runner = TkTaskRunner(root, poll_interval_ms=10, idle_poll_interval_ms=30)
        entered = threading.Event()
        release = threading.Event()

        def operation() -> str:
            entered.set()
            release.wait(timeout=1)
            return "done"

        runner.submit(
            "work",
            operation,
            on_success=lambda _value: None,
            on_error=lambda exc: self.fail(str(exc)),
        )
        immediate_id = next(iter(root.callbacks))
        self.assertTrue(entered.wait(timeout=1))

        root.fire(immediate_id)

        _busy_id, (busy_delay, _busy_callback) = next(iter(root.callbacks.items()))
        self.assertEqual(busy_delay, 10)
        release.set()
        runner.close()


if __name__ == "__main__":
    unittest.main()
