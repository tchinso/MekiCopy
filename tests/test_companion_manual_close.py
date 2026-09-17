from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

from companion_manual_close import (
    MANUAL_CLOSE_EXIT_CODE,
    consume_manual_close_signal,
    create_manual_close_signal,
    manual_close_signal_arguments,
    publish_manual_close_signal,
)
from companion_watchdog import RecoveryRequest
import mekicopy


class ManualCloseSignalTests(unittest.TestCase):
    def test_signed_notice_is_one_use_and_matches_the_launched_process(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            signal = create_manual_close_signal(
                "MekiScript",
                directory=Path(temporary_directory),
            )
            self.assertFalse(signal.path.exists())
            self.assertEqual(
                manual_close_signal_arguments(signal)[:3],
                ["--watchdog-manual-close-file", str(signal.path), "--watchdog-manual-close-token"],
            )
            self.assertTrue(
                publish_manual_close_signal(
                    signal.path,
                    signal.token,
                    app_name="MekiScript",
                    process_id=4242,
                )
            )
            self.assertTrue(
                consume_manual_close_signal(
                    signal,
                    app_name="MekiScript",
                    process_id=4242,
                )
            )
            self.assertFalse(signal.path.exists())
            self.assertFalse(
                consume_manual_close_signal(
                    signal,
                    app_name="MekiScript",
                    process_id=4242,
                )
            )

    def test_wrong_process_cannot_suppress_recovery(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            signal = create_manual_close_signal(
                "MekiOverlayer",
                directory=Path(temporary_directory),
            )
            self.assertTrue(
                publish_manual_close_signal(
                    signal.path,
                    signal.token,
                    app_name="MekiOverlayer",
                    process_id=111,
                )
            )
            self.assertFalse(
                consume_manual_close_signal(
                    signal,
                    app_name="MekiOverlayer",
                    process_id=222,
                )
            )
            self.assertFalse(signal.path.exists())


class MainWindowManualCloseTests(unittest.TestCase):
    @staticmethod
    def _request(*, exit_code: int | None = None) -> RecoveryRequest:
        return RecoveryRequest(
            name="MekiScript",
            generation=1,
            reason="process_exited",
            detail="test child exit",
            consecutive_failures=1,
            observed_at=1.0,
            exit_code=exit_code,
        )

    def test_confirmed_notice_untracks_before_recovery_relaunch(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            signal = create_manual_close_signal(
                "MekiScript",
                directory=Path(temporary_directory),
            )
            child = SimpleNamespace(pid=8675)
            self.assertTrue(
                publish_manual_close_signal(
                    signal.path,
                    signal.token,
                    app_name="MekiScript",
                    process_id=child.pid,
                )
            )
            watchdog = mock.Mock()
            window = SimpleNamespace(
                script_process=child,
                _companion_manual_close_signals={"MekiScript": signal},
                _companion_watchdog=watchdog,
                _launch_script=mock.Mock(),
            )

            with mock.patch.object(mekicopy, "_log_runtime_message"):
                mekicopy.MainWindow._recover_owned_companion(window, self._request())

            watchdog.untrack.assert_called_once_with("MekiScript")
            watchdog.matches_process.assert_not_called()
            window._launch_script.assert_not_called()
            self.assertEqual(window._companion_manual_close_signals, {})

    def test_manual_exit_code_is_a_file_failure_fallback(self) -> None:
        child = SimpleNamespace(pid=8675)
        watchdog = mock.Mock()
        watchdog.matches_process.return_value = True
        window = SimpleNamespace(
            script_process=child,
            _companion_manual_close_signals={},
            _companion_watchdog=watchdog,
            _launch_script=mock.Mock(),
        )

        with mock.patch.object(mekicopy, "_log_runtime_message"):
            mekicopy.MainWindow._recover_owned_companion(
                window,
                self._request(exit_code=MANUAL_CLOSE_EXIT_CODE),
            )

        watchdog.untrack.assert_called_once_with("MekiScript")
        window._launch_script.assert_not_called()

    def test_idle_companions_do_not_read_close_notice_files(self) -> None:
        child = SimpleNamespace(pid=8675, poll=mock.Mock(return_value=None))
        signal = mock.Mock()
        window = SimpleNamespace(
            script_process=child,
            _companion_manual_close_signals={"MekiScript": signal},
            _companion_watchdog=mock.Mock(),
        )

        with mock.patch.object(mekicopy, "consume_manual_close_signal") as consume:
            mekicopy.MainWindow._consume_pending_manual_close_signals(window)

        child.poll.assert_called_once_with()
        consume.assert_not_called()

    def test_unavailable_notice_directory_keeps_companion_launch_available(self) -> None:
        with (
            mock.patch.object(mekicopy, "create_manual_close_signal", side_effect=OSError("read-only")),
            mock.patch.object(mekicopy, "_log_runtime_error") as log_error,
        ):
            signal = mekicopy.MainWindow._create_manual_close_signal(
                SimpleNamespace(),
                "MekiScript",
            )

        self.assertIsNone(signal)
        log_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
