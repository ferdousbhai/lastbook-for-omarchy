#!/usr/bin/env python3
"""What the engine tells the window unasked about a setup job: each change of its state, so the window reads the job then
and asks on a timer only for progress. Against test_setup's local HTTPS server. No real internet, never the real home folder.

  python3 -B app/tests/test_job_notices.py [-v]

Never packaged."""
import sys, time, types, unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parents[1])]
import test_setup as T  # noqa: E402
_stdout = sys.stdout
import engine_pipe  # noqa: E402
sys.stdout = _stdout     # engine_pipe keeps standard output for its protocol; here it is the test run's again


class JobNotices(T.Base):
    def test_each_change_of_state_is_told(self):
        told = []
        self.files.pace = 0.01
        s = self.setup(on_job=told.append)
        s.start('small')
        time.sleep(0.3)
        self.assertIn('downloading', told)
        s.pause()
        self.assertEqual(told[-1], 'paused')
        self.files.pace = 0
        s.resume()
        self.assertEqual(self.wait(s, ('done', 'failed'))['state'], 'done')
        self.assertEqual(told[-1], 'done')
        self.assertIn('unpacking', told)
        s.cancel()                      # the staged library is dropped: the window hears of that too
        self.assertEqual(told[-1], 'failed')

    def test_a_window_that_cannot_be_told_does_not_stop_the_job(self):
        def gone(state):
            raise BrokenPipeError(32, 'Broken pipe')
        s = self.setup(on_job=gone)
        with mock.patch.object(T.api.traceback, 'print_exc'):
            s.start('small')
            self.assertEqual(self.wait(s)['state'], 'done')


class Pipe(unittest.TestCase):
    """app/engine_pipe.py's hook: the notice as the window reads it (native/src/setup.rs, `follow`)."""

    def hooks(self):
        return engine_pipe.options(types.SimpleNamespace(setup_options=dict, remember=lambda folder: None))

    def test_a_change_is_sent_as_a_line_with_no_id(self):
        sent = []
        with mock.patch.object(engine_pipe, 'send', sent.append):
            self.hooks()['on_job']('paused')
        self.assertEqual(sent, [{'job': 'paused'}])

    def test_a_closed_pipe_is_not_an_error(self):
        with mock.patch.object(engine_pipe, 'send', side_effect=BrokenPipeError(32, 'Broken pipe')):
            self.hooks()['on_job']('done')      # the window has gone; nothing is raised into the job


if __name__ == '__main__':
    unittest.main()
