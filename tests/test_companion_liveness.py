from __future__ import annotations

import unittest
from unittest import mock

import companion_liveness


class FakeTkRoot:
    """Minimal Tk-compatible scheduler that lets tests invoke queued callbacks."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, object]] = []
        self.fail_next_schedule = False

    def after(self, delay: int, callback: object) -> None:
        if self.fail_next_schedule:
            self.fail_next_schedule = False
            raise RuntimeError("Tk is shutting down")
        self.calls.append((delay, callback))


class UiHeartbeatTests(unittest.TestCase):
    def test_health_payload_is_fresh_after_a_recent_tick(self) -> None:
        with mock.patch.object(
            companion_liveness.time,
            "monotonic",
            side_effect=[100.0, 101.2346],
        ):
            heartbeat = companion_liveness.UiHeartbeat(max_age_seconds=2)
            payload = heartbeat.health_payload()

        self.assertEqual(
            payload,
            {
                "uiResponsive": True,
                "uiHeartbeatAgeSeconds": 1.235,
            },
        )

    def test_health_payload_is_stale_after_its_maximum_age(self) -> None:
        with mock.patch.object(
            companion_liveness.time,
            "monotonic",
            side_effect=[50.0, 55.001],
        ):
            heartbeat = companion_liveness.UiHeartbeat(max_age_seconds=5)
            payload = heartbeat.health_payload()

        self.assertEqual(
            payload,
            {
                "uiResponsive": False,
                "uiHeartbeatAgeSeconds": 5.001,
            },
        )

    def test_schedule_uses_tk_after_and_keeps_pulsing(self) -> None:
        heartbeat = companion_liveness.UiHeartbeat()
        root = FakeTkRoot()

        with mock.patch.object(heartbeat, "tick") as tick:
            heartbeat.schedule(root, interval_ms=5)

            self.assertEqual(len(root.calls), 1)
            initial_delay, pulse = root.calls.pop(0)
            self.assertEqual(initial_delay, 0)
            self.assertTrue(callable(pulse))

            pulse()
            self.assertEqual(tick.call_count, 1)
            self.assertEqual(len(root.calls), 1)
            repeat_delay, repeat_pulse = root.calls.pop(0)
            self.assertEqual(repeat_delay, 25)
            self.assertIs(repeat_pulse, pulse)

            repeat_pulse()
            self.assertEqual(tick.call_count, 2)
            self.assertEqual(root.calls[0][0], 25)

    def test_schedule_stops_quietly_when_tk_is_tearing_down(self) -> None:
        heartbeat = companion_liveness.UiHeartbeat()
        root = FakeTkRoot()

        with mock.patch.object(heartbeat, "tick") as tick:
            heartbeat.schedule(root)
            _delay, pulse = root.calls.pop(0)
            root.fail_next_schedule = True
            pulse()

        tick.assert_called_once_with()
        self.assertEqual(root.calls, [])

    def test_schedule_is_safe_when_called_during_tk_teardown(self) -> None:
        heartbeat = companion_liveness.UiHeartbeat()
        root = FakeTkRoot()
        root.fail_next_schedule = True

        heartbeat.schedule(root)

        self.assertEqual(root.calls, [])


if __name__ == "__main__":
    unittest.main()
