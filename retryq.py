#!/usr/bin/env python3
"""retryq - retry flaky commands with exponential backoff and full jitter.

Runs a command; if it exits non-zero, retries it with exponential backoff
(+ optional full jitter), per-attempt timeout, and an exit-code retry policy.

Exit codes:
    0    command eventually succeeded
    124  an attempt timed out (--timeout); also the exit code reported when
         the retry budget is exhausted on a timed-out attempt
    126  command found but could not be executed (e.g. permission denied)
    127  command not found (never retried)
    2    invalid arguments
    else passthrough of the last attempt's exit code

Standard library only. Python 3.9+.
"""

import argparse
import os
import random
import signal
import subprocess
import sys
import time

EXIT_TIMEOUT = 124
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127
EXIT_INVALID_ARGS = 2  # argparse exits 2 on invalid arguments (parser.error)

PROG = "retryq"


def _positive_int(value):
    iv = int(value)
    if iv < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return iv


def _positive_float(value):
    fv = float(value)
    if fv <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return fv


def _code_list(value):
    """Parse '1,2,5' into a set of ints (0-255)."""
    codes = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            raise argparse.ArgumentTypeError("empty entry in code list")
        try:
            code = int(part)
        except ValueError:
            raise argparse.ArgumentTypeError("not an integer: %r" % part)
        if not 0 <= code <= 255:
            raise argparse.ArgumentTypeError(
                "exit code out of range (0-255): %r" % part
            )
        codes.add(code)
    if not codes:
        raise argparse.ArgumentTypeError("code list is empty")
    return codes


def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Retry flaky commands with exponential backoff + full jitter.",
        epilog="Example: retryq --retries 5 --backoff 2 --max-delay 30 "
               "--jitter --timeout 60 -- ./flaky-deploy.sh",
    )
    p.add_argument("--retries", type=_positive_int, default=3,
                   help="number of retries after the first attempt (default: 3)")
    p.add_argument("--backoff", type=_positive_float, default=1.0,
                   help="base backoff in seconds (default: 1.0)")
    p.add_argument("--max-delay", type=_positive_float, default=60.0,
                   help="cap for a single backoff delay in seconds (default: 60.0)")
    j = p.add_mutually_exclusive_group()
    j.add_argument("--jitter", dest="jitter", action="store_true",
                   help="full jitter: each delay is uniform(0, computed) (default)")
    j.add_argument("--no-jitter", dest="jitter", action="store_false",
                   help="use exact exponential delays")
    p.set_defaults(jitter=True)
    p.add_argument("--timeout", type=_positive_float, default=None, metavar="SECONDS",
                   help="per-attempt timeout; kills the whole process group, "
                        "reports exit 124")
    p.add_argument("--max-time", type=_positive_float, default=None, metavar="SECONDS",
                   help="total time budget in seconds; stop retrying once the "
                        "budget would be exceeded")
    p.add_argument("--retry-on-codes", type=_code_list, default=None, metavar="1,2",
                   help="only retry when the exit code is in this list")
    p.add_argument("--no-retry-on-codes", type=_code_list, default=None, metavar="0",
                   help="never retry these exit codes (takes precedence over "
                        "--retry-on-codes)")
    v = p.add_mutually_exclusive_group()
    v.add_argument("--verbose", action="store_true",
                   help="extra detail in per-attempt logs")
    v.add_argument("--quiet", action="store_true",
                   help="suppress per-attempt logs")
    p.add_argument("--dry-run", action="store_true",
                   help="print the retry plan without executing anything")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="command to run, after '--'")
    return p


def compute_delay(retry_index, backoff, max_delay, jitter):
    """Delay before retry number `retry_index` (0-based)."""
    delay = min(max_delay, backoff * (2 ** retry_index))
    if jitter:
        delay = random.uniform(0, delay)
    return delay


def is_retryable(code, retry_on, no_retry_on):
    if code == 0:
        return False
    if no_retry_on is not None and code in no_retry_on:
        return False
    if retry_on is not None:
        return code in retry_on
    return True


def log(msg, quiet):
    if not quiet:
        print("%s: %s" % (PROG, msg), file=sys.stderr, flush=True)


def run_attempt(cmd, timeout):
    """Run one attempt. Returns (exit_code, timed_out).

    The child runs in its own process group so a timeout kills the whole
    tree (children included). 127 = command not found, 126 = not executable,
    130 = interrupted by Ctrl-C, 124 = timed out.
    """
    try:
        proc = subprocess.Popen(cmd, start_new_session=True)
    except FileNotFoundError:
        return (EXIT_NOT_FOUND, False)
    except PermissionError:
        return (EXIT_NOT_EXECUTABLE, False)
    except OSError as exc:
        print("%s: cannot execute command: %s" % (PROG, exc), file=sys.stderr)
        return (EXIT_NOT_EXECUTABLE, False)

    old_sigterm = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(signum, frame):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        # Re-raise with default disposition so the exit status is honest.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)

    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
            return (EXIT_TIMEOUT, True)
    except KeyboardInterrupt:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        return (130, False)
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)
    return (proc.returncode, False)


def print_plan(args, delays):
    print("%s: dry-run plan (nothing executed)" % PROG)
    print("  command      : %s" % " ".join(args.command))
    print("  attempts     : %d (1 initial + %d retries)"
          % (1 + args.retries, args.retries))
    if args.retries:
        print("  backoff      : base=%.3gs, cap=%.3gs, %s"
              % (args.backoff, args.max_delay,
                 "full jitter uniform(0, delay)" if args.jitter else "exact delays"))
        for i, d in enumerate(delays):
            print("  delay #%d     : %.3fs%s" % (
                i + 1, d, " (randomized at runtime)" if args.jitter else ""))
    print("  timeout      : %s" % ("%.3gs per attempt" % args.timeout
                                   if args.timeout else "none"))
    print("  max-time     : %s" % ("%.3gs total" % args.max_time
                                   if args.max_time else "none"))
    if args.retry_on_codes is not None:
        print("  retry-on     : %s" % sorted(args.retry_on_codes))
    if args.no_retry_on_codes is not None:
        print("  no-retry-on  : %s" % sorted(args.no_retry_on_codes))
    print("  note         : exit 127 (command not found) is never retried")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # argparse.REMAINDER keeps the leading "--" separator in the list; drop it.
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]

    if not args.command:
        parser.error("no command given (usage: retryq [options] -- <command>)")

    if args.max_delay < args.backoff:
        parser.error("--max-delay must be >= --backoff")

    max_attempts = 1 + args.retries

    if args.dry_run:
        delays = [compute_delay(i, args.backoff, args.max_delay, jitter=False)
                  for i in range(args.retries)]
        print_plan(args, delays)
        return 0

    start = time.monotonic()
    last_code = 1
    attempts_done = 0

    for attempt in range(1, max_attempts + 1):
        # Honor --max-time: shrink (or skip) the attempt if the budget is gone.
        remaining = None
        if args.max_time is not None:
            remaining = args.max_time - (time.monotonic() - start)
            if remaining <= 0:
                log("max-time budget exhausted after %d attempt(s); "
                    "stopping" % attempts_done, args.quiet)
                break
        timeout = args.timeout
        if remaining is not None and (timeout is None or timeout > remaining):
            timeout = remaining

        log("attempt %d/%d: %s" % (attempt, max_attempts, " ".join(args.command)),
            args.quiet)
        code, timed_out = run_attempt(args.command, timeout)
        attempts_done += 1
        last_code = code

        if code == EXIT_NOT_FOUND:
            log("command not found; not retrying (exit 127)", args.quiet)
            return EXIT_NOT_FOUND

        if timed_out:
            log("attempt %d timed out after %.3fs (exit 124, process group killed)"
                % (attempt, timeout if timeout else 0), args.quiet)
        elif args.verbose:
            log("attempt %d exited with code %d" % (attempt, code), args.quiet)
        else:
            log("attempt %d exit code %d" % (attempt, code), args.quiet)

        if code == 0:
            log("succeeded on attempt %d" % attempt, args.quiet)
            return 0

        if attempt == max_attempts:
            break

        if not is_retryable(code, args.retry_on_codes, args.no_retry_on_codes):
            log("exit code %d is not retryable by policy; stopping" % code,
                args.quiet)
            break

        delay = compute_delay(attempt - 1, args.backoff, args.max_delay, args.jitter)
        if args.max_time is not None:
            elapsed = time.monotonic() - start
            if elapsed + delay > args.max_time:
                log("next delay %.3fs would exceed --max-time; stopping "
                    "after %d attempt(s)" % (delay, attempts_done), args.quiet)
                break
        log("retrying in %.3fs..." % delay, args.quiet)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            log("interrupted during backoff; stopping", args.quiet)
            return 130

    log("giving up after %d attempt(s); final exit code %d"
        % (attempts_done, last_code), args.quiet)
    return last_code


if __name__ == "__main__":
    sys.exit(main())
