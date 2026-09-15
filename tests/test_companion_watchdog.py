from __future__ import annotations

import unittest

from companion_watchdog import (
    CompanionWatchdog,
    HealthProbeResult,
    default_health_evaluator,
)


class _Process:
    def __init__(self, exit_code: int | None = None) -> None:
        self.exit_code = exit_code

    def poll(self) -> int | None:
        return self.exit_code


class _Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class HealthEvaluatorTests(unittest.TestCase):
    def test_error_state_is_still_a_usable_control_endpoint(self) -> None:
        evaluate = default_health_evaluator("MekiAudioCapture", require_ui_responsive=True)
        self.assertIsNone(
            evaluate(
                {
                    "ok": True,
                    "app": "MekiAudioCapture",
                    "state": "ERROR",
                    "uiResponsive": True,
                }
            )
        )

    def test_stale_or_missing_required_gui_heartbeat_is_rejected(self) -> None:
        evaluate = default_health_evaluator(
            "MekiScript",
            require_ui_responsive=True,
            max_ui_heartbeat_age_seconds=5,
        )
        self.assertIn(
            "did not report",
            evaluate({"ok": True, "app": "MekiScript"}) or "",
        )
        self.assertIn(
            "not responding",
            evaluate(
                {"ok": True, "app": "MekiScript", "uiResponsive": False}
            )
            or "",
        )
        self.assertIn(
            "stale",
            evaluate(
                {
                    "ok": True,
                    "app": "MekiScript",
                    "uiResponsive": True,
                    "uiHeartbeatAgeSeconds": 5.1,
                }
            )
            or "",
        )


class CompanionWatchdogTests(unittest.TestCase):
    @staticmethod
    def _unhealthy_probe(*_args: object, **_kwargs: object) -> HealthProbeResult:
        return HealthProbeResult(False, "loopback health endpoint timed out")

    @staticmethod
    def _healthy_probe(*_args: object, **_kwargs: object) -> HealthProbeResult:
        return HealthProbeResult(True, payload={"ok": True})

    def test_live_process_requires_consecutive_probe_failures(self) -> None:
        clock = _Clock()
        watchdog = CompanionWatchdog(
            failures_before_recovery=3,
            cooldown_seconds=20,
            health_probe=self._unhealthy_probe,
            clock=clock,
        )
        generation = watchdog.track(
            "MekiScript",
            expected_app="MekiScript",
            base_url="http://127.0.0.1:6999",
            process=_Process(),
        )

        watchdog.run_once()
        watchdog.run_once()
        self.assertEqual(watchdog.drain_recovery_requests(), [])
        self.assertEqual(watchdog.status("MekiScript").consecutive_failures, 2)

        watchdog.run_once()
        requests = watchdog.drain_recovery_requests()
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.generation, generation)
        self.assertEqual(request.reason, "health_failed")
        self.assertEqual(request.consecutive_failures, 3)
        self.assertTrue(watchdog.status("MekiScript").recovering)

        # A queued recovery suppresses duplicate events until the UI finishes
        # its own terminate/restart operation.
        watchdog.run_once()
        self.assertEqual(watchdog.drain_recovery_requests(), [])

    def test_dead_owned_process_requests_recovery_immediately(self) -> None:
        watchdog = CompanionWatchdog(
            failures_before_recovery=99,
            health_probe=self._healthy_probe,
        )
        watchdog.track(
            "HYTrans",
            expected_app="HYTrans",
            base_url="http://127.0.0.1:6996",
            process=_Process(17),
        )

        watchdog.run_once()
        requests = watchdog.drain_recovery_requests()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].reason, "process_exited")
        self.assertEqual(requests[0].exit_code, 17)

    def test_new_process_makes_an_old_queued_request_stale(self) -> None:
        watchdog = CompanionWatchdog(
            failures_before_recovery=1,
            health_probe=self._unhealthy_probe,
        )
        old_process = _Process()
        old_generation = watchdog.track(
            "MekiAudioCapture",
            expected_app="MekiAudioCapture",
            base_url="http://127.0.0.1:6998",
            process=old_process,
        )
        watchdog.run_once()
        request = watchdog.drain_recovery_requests()[0]

        new_generation = watchdog.track(
            "MekiAudioCapture",
            expected_app="MekiAudioCapture",
            base_url="http://127.0.0.1:6998",
            process=_Process(),
        )
        self.assertNotEqual(old_generation, new_generation)
        self.assertFalse(watchdog.is_current(request))
        self.assertFalse(watchdog.acknowledge_recovery(request, succeeded=True))

    def test_failed_recovery_is_rate_limited_then_retried(self) -> None:
        clock = _Clock()
        watchdog = CompanionWatchdog(
            failures_before_recovery=1,
            cooldown_seconds=10,
            health_probe=self._unhealthy_probe,
            clock=clock,
        )
        watchdog.track(
            "MekiScript",
            expected_app="MekiScript",
            base_url="http://127.0.0.1:6999",
            process=_Process(),
        )
        watchdog.run_once()
        request = watchdog.drain_recovery_requests()[0]
        self.assertTrue(watchdog.acknowledge_recovery(request, succeeded=False))

        clock.now += 9.9
        watchdog.run_once()
        self.assertEqual(watchdog.drain_recovery_requests(), [])

        clock.now += 0.1
        watchdog.run_once()
        retry = watchdog.drain_recovery_requests()
        self.assertEqual(len(retry), 1)
        self.assertGreaterEqual(retry[0].consecutive_failures, 3)

    def test_success_clears_a_failure_streak_before_threshold(self) -> None:
        outcomes = iter(
            [
                HealthProbeResult(False, "first timeout"),
                HealthProbeResult(True),
                HealthProbeResult(False, "second timeout"),
            ]
        )

        def probe(*_args: object, **_kwargs: object) -> HealthProbeResult:
            return next(outcomes)

        watchdog = CompanionWatchdog(
            failures_before_recovery=2,
            health_probe=probe,
        )
        watchdog.track(
            "MekiScript",
            expected_app="MekiScript",
            base_url="http://127.0.0.1:6999",
            process=_Process(),
        )
        watchdog.run_once()
        self.assertEqual(watchdog.status("MekiScript").consecutive_failures, 1)
        watchdog.run_once()
        self.assertEqual(watchdog.status("MekiScript").consecutive_failures, 0)
        watchdog.run_once()
        self.assertEqual(watchdog.drain_recovery_requests(), [])
        self.assertEqual(watchdog.status("MekiScript").consecutive_failures, 1)

    def test_repeating_same_registration_does_not_clear_recovery_state(self) -> None:
        watchdog = CompanionWatchdog(
            failures_before_recovery=1,
            health_probe=self._unhealthy_probe,
        )
        process = _Process()
        first = watchdog.track(
            "MekiScript",
            expected_app="MekiScript",
            base_url="http://127.0.0.1:6999",
            process=process,
        )
        watchdog.run_once()
        second = watchdog.track(
            "MekiScript",
            expected_app="MekiScript",
            base_url="http://127.0.0.1:6999",
            process=process,
        )
        self.assertEqual(first, second)
        self.assertTrue(watchdog.status("MekiScript").recovering)

    def test_recovered_companion_cancels_an_unclaimed_request(self) -> None:
        outcomes = iter(
            [
                HealthProbeResult(False, "temporary overload"),
                HealthProbeResult(True),
            ]
        )

        def probe(*_args: object, **_kwargs: object) -> HealthProbeResult:
            return next(outcomes)

        watchdog = CompanionWatchdog(
            failures_before_recovery=1,
            health_probe=probe,
        )
        watchdog.track(
            "MekiAudioCapture",
            expected_app="MekiAudioCapture",
            base_url="http://127.0.0.1:6998",
            process=_Process(),
        )

        watchdog.run_once()
        request = watchdog.drain_recovery_requests()[0]
        self.assertTrue(watchdog.status("MekiAudioCapture").recovering)

        # The process recovers before the Tk callback claims the queued item.
        watchdog.run_once()
        status = watchdog.status("MekiAudioCapture")
        self.assertFalse(status.recovering)
        self.assertEqual(status.consecutive_failures, 0)
        self.assertFalse(watchdog.claim_recovery(request))

    def test_claimed_recovery_is_not_canceled_mid_restart(self) -> None:
        outcomes = iter(
            [
                HealthProbeResult(False, "temporary overload"),
                HealthProbeResult(True),
            ]
        )

        def probe(*_args: object, **_kwargs: object) -> HealthProbeResult:
            return next(outcomes)

        watchdog = CompanionWatchdog(
            failures_before_recovery=1,
            health_probe=probe,
        )
        watchdog.track(
            "MekiScript",
            expected_app="MekiScript",
            base_url="http://127.0.0.1:6999",
            process=_Process(),
        )

        watchdog.run_once()
        request = watchdog.drain_recovery_requests()[0]
        self.assertTrue(watchdog.claim_recovery(request))
        watchdog.run_once()

        self.assertTrue(watchdog.status("MekiScript").recovering)
        self.assertTrue(watchdog.acknowledge_recovery(request, succeeded=True))

    def test_request_must_match_the_tracked_owned_process(self) -> None:
        watchdog = CompanionWatchdog(
            failures_before_recovery=1,
            health_probe=self._unhealthy_probe,
        )
        child = _Process()
        watchdog.track(
            "MekiOverlayer",
            expected_app="MekiOverlayer",
            base_url="http://127.0.0.1:6997",
            process=child,
        )
        watchdog.run_once()
        request = watchdog.drain_recovery_requests()[0]

        self.assertTrue(watchdog.matches_process(request, child))
        self.assertFalse(watchdog.matches_process(request, _Process()))


if __name__ == "__main__":
    unittest.main()
