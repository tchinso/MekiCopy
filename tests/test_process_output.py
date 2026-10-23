from __future__ import annotations

import subprocess
import sys
import threading
import time
import unittest

from process_output import iter_process_lines


class ProcessOutputTests(unittest.TestCase):
    def test_cancellation_interrupts_a_child_that_never_outputs(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        cancelled = threading.Event()
        timer = threading.Timer(0.15, cancelled.set)
        timer.start()

        def check():
            if cancelled.is_set():
                raise InterruptedError("cancelled")

        started = time.monotonic()
        try:
            with self.assertRaises(InterruptedError):
                list(iter_process_lines(child.stdout, check))
            self.assertLess(time.monotonic() - started, 3)
        finally:
            child.kill()
            child.wait(timeout=5)
            child.stdout.close()
            timer.cancel()

    def test_delivers_output_in_order_through_eof(self):
        import io

        self.assertEqual(
            list(iter_process_lines(io.StringIO("first\nsecond\n"), lambda: None)),
            ["first\n", "second\n"],
        )
