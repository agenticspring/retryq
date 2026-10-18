#!/usr/bin/env python3
"""Test suite for retryq. Run: python3 -m unittest discover -s tests"""
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RETRYQ = os.path.join(ROOT, "retryq.py")
FLAKY = os.path.join(HERE, "helpers", "flaky.py")
SLEEPER = os.path.join(HERE, "helpers", "sleeper.py")


def run_retryq(*args, timeout=60):
    """Run the retryq CLI; return (exit_code, stdout, stderr, elapsed)."""
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, RETRYQ] + list(args),
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0


def counter_value(path):
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


class TestRetryBasics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.counter = os.path.join(self.tmp.name, "counter.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def flaky_args(self, fail_times, exit_code=1, extra=()):
        return list(extra) + ["--", sys.executable, FLAKY,
                              "--counter", self.counter,
                              "--fail-times", str(fail_times),
                              "--exit-code", str(exit_code)]

    def test_success_after_n_retries(self):
        code, out, err, _ = run_retryq(
            "--retries", "3", "--backoff", "0.01", "--no-jitter",
            *self.flaky_args(fail_times=2))
        self.assertEqual(code, 0)
        self.assertEqual(counter_value(self.counter), 3)
        self.assertIn("attempt 3/4", err)

    def test_immediate_success_no_retry(self):
        code, out, err, _ = run_retryq(
            "--retries", "3", "--backoff", "0.01", "--no-jitter",
            *self.flaky_args(fail_times=0))
        self.assertEqual(code, 0)
        self.assertEqual(counter_value(self.counter), 1)
        self.assertIn("succeeded on attempt 1", err)

    def test_retry_exhausted_passthrough_exit_code(self):
        code, out, err, _ = run_retryq(
            "--retries", "2", "--backoff", "0.01", "--no-jitter",
            *self.flaky_args(fail_times=10, exit_code=5))
        self.assertEqual(code, 5)
        self.assertEqual(counter_value(self.counter), 3)  # 1 + 2 retries
        self.assertIn("giving up after 3 attempt(s)", err)

    def test_zero_retries_single_attempt(self):
        code, _, _, _ = run_retryq(
            "--retries", "0", *self.flaky_args(fail_times=5, exit_code=7))
        self.assertEqual(code, 7)
        self.assertEqual(counter_value(self.counter), 1)

    def test_command_not_found_exit_127_no_retry(self):
        code, out, err, _ = run_retryq(
            "--retries", "3", "--backoff", "0.01",
            "--", "./definitely-not-a-real-binary-retryq-xyz")
        self.assertEqual(code, 127)
        self.assertIn("not retrying", err)

    def test_invalid_args_exit_2(self):
        code, _, err, _ = run_retryq("--retries", "-1", "--", "true")
        self.assertEqual(code, 2)
        code, _, _, _ = run_retryq()  # no command at all
        self.assertEqual(code, 2)
        code, _, _, _ = run_retryq("--backoff", "0", "--", "true")
        self.assertEqual(code, 2)
        code, _, _, _ = run_retryq("--retry-on-codes", "abc", "--", "true")
        self.assertEqual(code, 2)
        code, _, _, _ = run_retryq("--max-delay", "0.1", "--backoff", "1",
                                   "--", "true")  # max-delay < backoff
        self.assertEqual(code, 2)


class TestRetryPolicyCodes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.counter = os.path.join(self.tmp.name, "counter.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def base(self, exit_code, extra=()):
        return list(extra) + ["--", sys.executable, FLAKY,
                              "--counter", self.counter,
                              "--fail-times", "99", "--exit-code", str(exit_code)]

    def test_retry_on_codes_allows(self):
        code, _, _, _ = run_retryq(
            "--retries", "1", "--backoff", "0.01", "--no-jitter",
            "--retry-on-codes", "1,2", *self.base(2))
        self.assertEqual(code, 2)
        self.assertEqual(counter_value(self.counter), 2)

    def test_retry_on_codes_blocks(self):
        code, _, err, _ = run_retryq(
            "--retries", "5", "--backoff", "0.01", "--no-jitter",
            "--retry-on-codes", "1", *self.base(2))
        self.assertEqual(code, 2)
        self.assertEqual(counter_value(self.counter), 1)
        self.assertIn("not retryable by policy", err)

    def test_no_retry_on_codes_blocks(self):
        code, _, _, _ = run_retryq(
            "--retries", "5", "--backoff", "0.01", "--no-jitter",
            "--no-retry-on-codes", "2", *self.base(2))
        self.assertEqual(code, 2)
        self.assertEqual(counter_value(self.counter), 1)

    def test_no_retry_on_codes_takes_precedence(self):
        # exit 2 is in the allow-list but also in the deny-list -> no retry
        code, _, _, _ = run_retryq(
            "--retries", "5", "--backoff", "0.01", "--no-jitter",
            "--retry-on-codes", "1,2", "--no-retry-on-codes", "2",
            *self.base(2))
        self.assertEqual(code, 2)
        self.assertEqual(counter_value(self.counter), 1)


class TestTimeout(unittest.TestCase):
    def test_timeout_kills_process_group_exit_124(self):
        tmp = tempfile.TemporaryDirectory()
        pidfile = os.path.join(tmp.name, "child.pid")
        code, out, err, elapsed = run_retryq(
            "--retries", "0", "--timeout", "0.5",
            "--", sys.executable, SLEEPER, pidfile)
        self.assertEqual(code, 124)
        self.assertLess(elapsed, 10)
        self.assertIn("timed out", err)
        # the grandchild (separate process in the same group) must be dead
        self.assertTrue(os.path.exists(pidfile), "child never started")
        with open(pidfile) as f:
            child_pid = int(f.read().strip())
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("grandchild process survived the process-group kill")
        tmp.cleanup()

    def test_timeout_attempt_is_retried_then_gives_up(self):
        tmp = tempfile.TemporaryDirectory()
        pidfile = os.path.join(tmp.name, "child.pid")
        code, _, err, _ = run_retryq(
            "--retries", "1", "--timeout", "0.3", "--backoff", "0.01",
            "--no-jitter", "--", sys.executable, SLEEPER, pidfile)
        self.assertEqual(code, 124)
        self.assertIn("attempt 2/2", err)
        tmp.cleanup()


class TestBackoffTiming(unittest.TestCase):
    def test_no_jitter_delays_are_exact(self):
        tmp = tempfile.TemporaryDirectory()
        counter = os.path.join(tmp.name, "counter.txt")
        code, _, _, elapsed = run_retryq(
            "--retries", "2", "--backoff", "0.1", "--max-delay", "10",
            "--no-jitter",
            "--", sys.executable, FLAKY, "--counter", counter,
            "--fail-times", "99", "--exit-code", "3")
        self.assertEqual(code, 3)
        # delays: 0.1 then 0.2 -> ~0.3s of sleep total
        self.assertGreaterEqual(elapsed, 0.28)
        self.assertLess(elapsed, 3.0)
        tmp.cleanup()

    def test_max_delay_caps_backoff(self):
        tmp = tempfile.TemporaryDirectory()
        counter = os.path.join(tmp.name, "counter.txt")
        code, _, _, elapsed = run_retryq(
            "--retries", "4", "--backoff", "0.5", "--max-delay", "0.5",
            "--no-jitter",
            "--", sys.executable, FLAKY, "--counter", counter,
            "--fail-times", "99", "--exit-code", "3")
        self.assertEqual(code, 3)
        # raw delays would be 0.5,1,2,4 -> capped to 0.5 each -> ~2.0s total
        self.assertGreaterEqual(elapsed, 1.8)
        self.assertLess(elapsed, 4.0)
        tmp.cleanup()

    def test_jitter_bounds(self):
        tmp = tempfile.TemporaryDirectory()
        counter = os.path.join(tmp.name, "counter.txt")
        code, _, _, elapsed = run_retryq(
            "--retries", "2", "--backoff", "0.2", "--max-delay", "10",
            "--jitter",
            "--", sys.executable, FLAKY, "--counter", counter,
            "--fail-times", "99", "--exit-code", "3")
        self.assertEqual(code, 3)
        # full jitter: each delay in [0, 0.2] / [0, 0.4] -> total < 0.6
        self.assertLess(elapsed, 2.0)
        tmp.cleanup()


class TestMaxTime(unittest.TestCase):
    def test_max_time_stops_before_next_delay(self):
        tmp = tempfile.TemporaryDirectory()
        counter = os.path.join(tmp.name, "counter.txt")
        code, _, err, _ = run_retryq(
            "--retries", "10", "--backoff", "5", "--no-jitter",
            "--max-time", "0.3",
            "--", sys.executable, FLAKY, "--counter", counter,
            "--fail-times", "99", "--exit-code", "9")
        self.assertEqual(code, 9)
        self.assertEqual(counter_value(counter), 1)  # no retry: delay 5s > budget
        self.assertIn("--max-time", err)
        tmp.cleanup()


class TestDryRun(unittest.TestCase):
    def test_dry_run_does_not_execute(self):
        tmp = tempfile.TemporaryDirectory()
        marker = os.path.join(tmp.name, "marker.txt")
        code, out, err, _ = run_retryq(
            "--retries", "3", "--backoff", "2", "--dry-run",
            "--", sys.executable, "-c",
            "open(%r, 'w').write('x')" % marker)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(marker), "dry-run executed the command!")
        self.assertIn("dry-run plan", out)
        self.assertIn("nothing executed", out)
        tmp.cleanup()


class TestLogging(unittest.TestCase):
    def test_quiet_suppresses_logs(self):
        code, out, err, _ = run_retryq(
            "--retries", "1", "--backoff", "0.01", "--no-jitter", "--quiet",
            "--", sys.executable, "-c", "import sys; sys.exit(4)")
        self.assertEqual(code, 4)
        self.assertEqual(err, "")

    def test_verbose_logs_exit_codes(self):
        code, out, err, _ = run_retryq(
            "--retries", "1", "--backoff", "0.01", "--no-jitter", "--verbose",
            "--", sys.executable, "-c", "import sys; sys.exit(4)")
        self.assertEqual(code, 4)
        self.assertIn("exited with code 4", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
