# retryq

Retry flaky commands with exponential backoff and full jitter. Zero dependencies.

```bash
retryq --retries 5 --backoff 2 --max-delay 30 --jitter --timeout 60 -- ./flaky-deploy.sh
```

Runs the command; on a non-zero exit it waits and retries, with the delay
growing exponentially (`backoff * 2^n`, capped by `--max-delay`) and
randomized with full jitter (`uniform(0, delay)`) to avoid thundering herds.
Built for CI pipelines, cron jobs, and deploy scripts that fail transiently.

## Features

- `--retries N` — retries after the first attempt (default 3)
- Exponential backoff: `--backoff` base seconds, `--max-delay` cap
- `--jitter` / `--no-jitter` — full jitter (default on), the AWS-recommended
  strategy for decorrelating retries
- `--timeout S` — per-attempt timeout; kills the **whole process group**
  (children included), reports exit code **124**
- `--max-time S` — total time budget; stops before the budget is exceeded
- `--retry-on-codes 1,2` / `--no-retry-on-codes 0` — which exit codes may be
  retried (`--no-retry-on-codes` wins on conflict)
- `--quiet` / `--verbose` — per-attempt logs (attempt number, exit code, delay)
- `--dry-run` — print the retry plan without executing anything
- Honest exit codes: `0` success, last attempt's code on exhaustion,
  `124` timeout, `126` not executable, `127` command not found, `2` bad args
- Ctrl-C / SIGTERM safe: the child process group is killed, no orphans

## Installation

Requires Python 3.9+. No third-party dependencies.

```bash
# from source
pip install git+https://github.com/agenticspring/retryq.git
# or just grab the single file — it runs standalone:
curl -O https://raw.githubusercontent.com/agenticspring/retryq/main/retryq.py
python3 retryq.py -- -- ./flaky.sh
```

## Usage

```bash
# Basic: retry a flaky deploy up to 5 times
retryq --retries 5 -- ./deploy.sh

# CI-friendly: short backoff, timeout per attempt, quiet
retryq --retries 4 --backoff 1 --max-delay 10 --timeout 120 --quiet -- ./e2e.sh

# Only retry on specific codes (e.g. 1 = transient, 2 = auth -> don't retry)
retryq --retries 3 --retry-on-codes 1 -- ./migrate.sh

# Never retry usage errors, retry everything else
retryq --retries 3 --no-retry-on-codes 2 -- ./job.sh

# Total budget of 5 minutes, deterministic delays
retryq --retries 10 --max-time 300 --no-jitter --verbose -- ./poll-api.sh

# Preview without running anything
retryq --retries 5 --backoff 2 --max-delay 30 --dry-run -- ./flaky-deploy.sh
```

Dry-run output:

```
retryq: dry-run plan (nothing executed)
  command      : ./flaky-deploy.sh
  attempts     : 6 (1 initial + 5 retries)
  backoff      : base=2s, cap=30s, full jitter uniform(0, delay)
  delay #1     : 2.000s (randomized at runtime)
  delay #2     : 4.000s (randomized at runtime)
  delay #3     : 8.000s (randomized at runtime)
  delay #4     : 16.000s (randomized at runtime)
  delay #5     : 30.000s (randomized at runtime)
  timeout      : none
  max-time     : none
  note         : exit 127 (command not found) is never retried
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | command eventually succeeded |
| 124  | an attempt timed out (`--timeout`); process group was killed |
| 126  | command found but not executable |
| 127  | command not found (never retried) |
| 2    | invalid arguments |
| 130  | interrupted (Ctrl-C) |
| other | passthrough of the last attempt's exit code |

## retryq vs cronwrap

`retryq` is **not** a duplicate of [cronwrap](https://github.com/agenticspring/cronwrap)
(the day-1 mission project). They solve different problems and compose:

| | cronwrap | retryq |
|---|---|---|
| Purpose | **Alert + log** wrapper for scheduled jobs | **Retry policy** wrapper for flaky commands |
| Core feature | Telegram/Discord alerts, log rotation | Exponential backoff + jitter retries |
| On failure | notifies you | tries again, then gives up |
| Timeout | yes (process-group kill, exit 124) | yes (process-group kill, exit 124) |
| Notifications | yes | no — stdout/stderr only |

Use them together — alerting around retries:

```bash
cronwrap --job-name nightly-deploy -- retryq --retries 5 --timeout 300 -- ./deploy.sh
```

`cronwrap` tells you when the job ultimately fails; `retryq` makes transient
failures not fail at all.

## Limitations

- Retries are **not idempotent-safe by magic**: only wrap commands that are
  safe to run more than once (or make them idempotent yourself).
- Jitter uses `random.uniform` (not cryptographically secure); fine for
  backoff timing, not for security decisions.
- `--timeout` kills with SIGKILL after the deadline — the command gets no
  graceful shutdown window.
- Exit code 127 (command not found) is never retried, even if listed in
  `--retry-on-codes`.
- Millisecond timing precision is best-effort (OS scheduling applies).

## Development

```bash
python3 -m unittest discover -s tests   # full suite, 19 tests
```

## License

MIT — see [LICENSE](LICENSE).
