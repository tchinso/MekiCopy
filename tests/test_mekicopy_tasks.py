import threading
import time
import unittest
from unittest import mock

from PySide6.QtCore import QObject

from mekicopy_qt_tasks import QtTaskRunner
from mekicopy_theme import ensure_application


class QtTaskRunnerTests(unittest.TestCase):
    def setUp(self):
        self.app = ensure_application()
        self.owner = QObject()
        self.runner = QtTaskRunner(self.owner)
        self.addCleanup(self.runner.close)

    def pump_until(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.assertTrue(predicate())

    def test_callbacks_execute_on_gui_thread_after_background_processing(self):
        gui_thread = threading.get_ident()
        completed = []
        self.assertTrue(self.runner.submit(
            "work", threading.get_ident,
            on_success=lambda worker: completed.append((worker, threading.get_ident())),
            on_error=self.fail,
        ))
        self.assertTrue(self.runner.is_running("work"))
        self.pump_until(lambda: bool(completed))
        worker, callback = completed[0]
        self.assertNotEqual(worker, gui_thread)
        self.assertEqual(callback, gui_thread)
        self.assertFalse(self.runner.is_running("work"))

    def test_fifo_admission_error_delivery_and_capacity(self):
        self.runner.MAX_WORKERS = 1
        self.runner.MAX_WAITING = 1
        release = threading.Event()
        self.addCleanup(release.set)
        values, errors = [], []
        callbacks = dict(on_success=values.append, on_error=errors.append)
        self.assertTrue(self.runner.submit("first", lambda: release.wait(2), **callbacks))
        self.assertFalse(self.runner.submit("first", mock.Mock(), **callbacks))
        self.assertTrue(self.runner.submit("second", mock.Mock(side_effect=ValueError("failed")), **callbacks))
        self.assertFalse(self.runner.submit("overflow", mock.Mock(), **callbacks))
        self.assertTrue(self.runner.is_running("second"))
        release.set()
        self.pump_until(lambda: not self.runner.is_running("second"))
        self.assertEqual(values, [True])
        self.assertEqual([str(error) for error in errors], ["failed"])
        self.assertTrue(self.runner.submit("first", lambda: 7, **callbacks))
        self.pump_until(lambda: values[-1] == 7)

    def test_close_discards_pending_work_and_late_completions(self):
        self.runner.MAX_WORKERS = 1
        release = threading.Event()
        finished = threading.Event()
        self.addCleanup(release.set)

        def operation():
            release.wait(2)
            finished.set()

        callback, queued = mock.Mock(), mock.Mock()
        arguments = dict(on_success=callback, on_error=callback)
        self.assertTrue(self.runner.submit("active", operation, **arguments))
        self.assertTrue(self.runner.submit("queued", queued, **arguments))
        self.runner.close()
        release.set()
        self.assertTrue(finished.wait(1))
        self.app.processEvents()
        queued.assert_not_called()
        callback.assert_not_called()
        self.assertFalse(self.runner.submit("later", queued, **arguments))

    def test_failed_thread_start_releases_task_key(self):
        with mock.patch("mekicopy_qt_tasks.threading.Thread.start", side_effect=RuntimeError("no thread")):
            with self.assertRaisesRegex(RuntimeError, "no thread"):
                self.runner.submit("work", mock.Mock(), on_success=self.fail, on_error=self.fail)
        self.assertFalse(self.runner.is_running("work"))
