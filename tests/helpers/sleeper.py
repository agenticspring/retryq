#!/usr/bin/env python3
"""Sleeps a long time while a CHILD process also sleeps.

Used to prove retryq --timeout kills the whole process group, not just the
direct child. The child writes its pid to `pidfile` immediately on startup.
"""
import os
import subprocess
import sys
import time


def main():
    pidfile = sys.argv[1]
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import os, time; "
         "open(%r, 'w').write(str(os.getpid())); "
         "time.sleep(60)" % pidfile]
    )
    try:
        time.sleep(60)
    finally:
        child.wait()


if __name__ == "__main__":
    main()
