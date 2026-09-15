from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

from companion_watchdog import RecoveryRequest
import mekicopy


def _request(name: str) -> RecoveryRequest:
    return RecoveryRequest(
        name=name,
        generation=1,
        reason="health_failed",
        detail="test health failure",
        consecutive_failures=3,
        observed_at=1.0,
    )


class MainWindowCompanionRecoveryTests(unittest.TestCase):
    @staticmethod
    def _window(**attributes: object) -> SimpleNamespace:
        defaults: dict[str, object] = {
            "audio_capture_process": None,
            "script_process": None,
            "overlayer_process": None,
            "hytrans_process": None,
            "_companion_watchdog": mock.Mock(),
            "_set_capture_status": mock.Mock(),
            "_launch_audio_capture": mock.Mock(return_value=True),
            "_launch_script": mock.Mock(return_value=True),
            "_launch_overlayer": mock.Mock(return_value=True),
            "_launch_hytrans": mock.Mock(return_value=True),
        }
        defaults.update(attributes)
        return SimpleNamespace(**defaults)

    def test_live_script_child_is_terminated_then_silently_relaunched(self) -> None:
        child = object()
        window = self._window(script_process=child)
        request = _request("MekiScript")

        with (
            mock.patch.object(mekicopy, "_log_runtime_message"),
            mock.patch.object(
                mekicopy,
                "_is_process_alive",
                side_effect=[True, False],
            ) as is_alive,
            mock.patch.object(mekicopy, "_terminate_process_tree") as terminate,
        ):
            mekicopy.MainWindow._recover_owned_companion(window, request)

        terminate.assert_called_once_with(child)
        window._launch_script.assert_called_once_with(notify=False)
        window._launch_audio_capture.assert_not_called()
        window._launch_overlayer.assert_not_called()
        window._launch_hytrans.assert_not_called()
        window._companion_watchdog.acknowledge_recovery.assert_not_called()
        self.assertIsNone(window.script_process)
        self.assertEqual(is_alive.call_count, 2)

    def test_dead_hytrans_child_is_silently_relaunched_as_restart(self) -> None:
        child = object()
        window = self._window(hytrans_process=child)
        request = _request("HYTrans")

        with (
            mock.patch.object(mekicopy, "_log_runtime_message"),
            mock.patch.object(mekicopy, "_is_process_alive", return_value=False),
            mock.patch.object(mekicopy, "_terminate_process_tree") as terminate,
        ):
            mekicopy.MainWindow._recover_owned_companion(window, request)

        terminate.assert_not_called()
        window._launch_hytrans.assert_called_once_with(restarted=True, notify=False)
        window._launch_audio_capture.assert_not_called()
        window._launch_script.assert_not_called()
        window._launch_overlayer.assert_not_called()
        window._companion_watchdog.acknowledge_recovery.assert_not_called()
        self.assertIsNone(window.hytrans_process)

    def test_failed_relaunch_acknowledges_failed_watchdog_recovery(self) -> None:
        request = _request("MekiScript")
        window = self._window(
            script_process=object(),
            _launch_script=mock.Mock(return_value=False),
        )

        with (
            mock.patch.object(mekicopy, "_log_runtime_message"),
            mock.patch.object(mekicopy, "_is_process_alive", return_value=False),
            mock.patch.object(mekicopy, "_terminate_process_tree") as terminate,
        ):
            mekicopy.MainWindow._recover_owned_companion(window, request)

        terminate.assert_not_called()
        window._launch_script.assert_called_once_with(notify=False)
        window._companion_watchdog.acknowledge_recovery.assert_called_once_with(
            request,
            succeeded=False,
        )

    def test_stale_window_handle_is_never_terminated(self) -> None:
        child = object()
        watchdog = mock.Mock()
        watchdog.matches_process.return_value = False
        window = self._window(
            script_process=child,
            _companion_watchdog=watchdog,
        )
        request = _request("MekiScript")

        with (
            mock.patch.object(mekicopy, "_log_runtime_message"),
            mock.patch.object(mekicopy, "_is_process_alive") as is_alive,
            mock.patch.object(mekicopy, "_terminate_process_tree") as terminate,
        ):
            mekicopy.MainWindow._recover_owned_companion(window, request)

        is_alive.assert_not_called()
        terminate.assert_not_called()
        window._launch_script.assert_not_called()
        watchdog.acknowledge_recovery.assert_called_once_with(request, succeeded=False)

    def test_silent_recovery_startup_failure_does_not_open_a_popup(self) -> None:
        child = mock.Mock()
        child.poll.return_value = 1
        window = SimpleNamespace(
            _closing=False,
            after=lambda _delay, callback: callback(),
        )

        with (
            mock.patch.object(mekicopy, "_log_runtime_message"),
            mock.patch.object(mekicopy.messagebox, "showerror") as showerror,
        ):
            mekicopy.MainWindow._watch_started_process(
                window,
                "MekiScript",
                child,
                notify=False,
            )

        showerror.assert_not_called()


if __name__ == "__main__":
    unittest.main()
