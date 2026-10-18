#!/usr/bin/env python3
"""Fake flaky command for retryq tests.

Fails `fail_times` times with `exit_code`, then succeeds. State is kept in a
counter file so retries are observable across process invocations.
"""
import argparse
import os
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--counter", required=True)
    p.add_argument("--fail-times", type=int, default=1)
    p.add_argument("--exit-code", type=int, default=1)
    args = p.parse_args()

    try:
        with open(args.counter) as f:
            count = int(f.read().strip() or 0)
    except (OSError, ValueError):
        count = 0

    count += 1
    with open(args.counter, "w") as f:
        f.write(str(count))

    if count <= args.fail_times:
        print("flaky attempt %d: failing with %d" % (count, args.exit_code))
        sys.exit(args.exit_code)
    print("flaky attempt %d: success" % count)
    sys.exit(0)


if __name__ == "__main__":
    main()
