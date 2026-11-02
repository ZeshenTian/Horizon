# Morning delivery timing and safe recovery

## What happened on 2026-11-01

[Scheduled run 41269557141](https://github.com/ZeshenTian/Horizon/actions/runs/41269557141)
was configured for 00:00 UTC / 08:00 Beijing, but GitHub created it at
03:31:59 UTC. The generation/delivery step ran from 03:32:16 to 03:41:20 UTC;
the complete workflow finished at 03:41:25. The large delay was **before the
application started**, not an SMTP retry or a three-hour model call. The API
identifies its event as `schedule`, not a manual dispatch.

GitHub does not expose the internal cause of that particular scheduler delay.
Its [schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
warns that high load can delay or drop scheduled jobs, especially at the start
of an hour. Moving a cron alone cannot guarantee delivery at a precise time.

### First live verification on 2026-11-02

[Run 41384335012](https://github.com/ZeshenTian/Horizon/actions/runs/41384335012)
was not created until 03:40:02 UTC / 11:40:02 Beijing. At 03:40:10 UTC the new
history guard raised `ValueError`, before dependency installation, generation
or email. This was a regression in the guard, separate from GitHub's continued
scheduler delay; the first patch did **not** establish punctual delivery.

A [no-send live API canary](https://github.com/ZeshenTian/Horizon/actions/runs/41385199633)
reproduced the failure: GitHub returns a future step as `status: pending` with
null `started_at`, `completed_at` and `conclusion`. The original unit fixtures
used only `queued`, so they missed that real response. The corrected guard
accepts both explicitly unstarted states, but still blocks timing evidence or
fails closed on unknown/inconsistent state. Its fixed safety reasons are now
visible without exposing API payloads or credentials.

The fixture in `tests/fixtures/github_pending_step.json` retains the observed
fields and source. CI now also checks real, in-progress GitHub API metadata
against a harmless future `echo` step. That canary has no model/mail secrets and
cannot send a digest. It supplements rather than replaces offline safety tests.

## Mitigation

- Keep the original 00:00 UTC / **08:00 Beijing** primary trigger and the fixed
  preceding 08:00-to-08:00 publication window.
- Add independent backup triggers at **08:07, 08:27, 08:47 and 09:17 Beijing**.
  A backup may start before a delayed primary. All triggers still depend on
  GitHub's scheduler, so all of them can be late or dropped.
- Serialize scheduled and manual runs in the same concurrency group. A later
  trigger does not cancel an active generation/delivery run. GitHub can replace
  a pending run with a newer pending run; every surviving run checks state again.
- Pin the issue to the run's **creation timestamp**, not runner start time, so
  an overnight runner queue does not silently change the issue date. GitHub
  does not provide the original nominal schedule timestamp; a schedule event
  itself delayed across an entire day cannot be unambiguously reconstructed.
- Check the existing date-keyed successful-delivery cache before installing
  dependencies or invoking MiniMax. Cache hits finish cheaply without sending.
- On a cache miss, inspect same-issue runs and **all job attempts**, including
  earlier attempts of the current run. A prior generation/delivery attempt or
  another active run prevents an automatic resend. This protects against an
  evicted cache or a failure after SMTP accepted the email but before the cache
  was saved. Errors or incomplete history fail closed rather than authorizing
  a send. Only the default branch can send through this workflow.
- Record the issue cutoff and run creation time in the Actions summary so
  scheduler delay and application runtime can be distinguished next time.

The workflow token adds only `actions: read`; `contents: read` remains unchanged.
No new secret, external scheduler, paid service, or extra recipient is required.
Backups after a confirmed delivery do not invoke the model again. Additional lightweight Actions
runs still consume runner time subject to the repository's existing plan.

## Conservative failure behavior

`Generate and send morning email` is one combined step. Once it has started,
the guard cannot reliably distinguish a model failure before SMTP from a
partially accepted email. Therefore a backup **does not retry that issue** even
if the step failed. Failures before that step (checkout, setup, configuration)
may be retried by a later trigger. A cache hit or a guard warning with skipped
delivery is not evidence that the current run sent a new message.

For an uncertain issue:

1. Follow the run ID in the guard warning; inspect all attempts and its logs.
2. Check Gmail Sent and the recipient inbox for the issue subject/date. SMTP
   acceptance and inbox delivery are distinct; do not infer non-delivery merely
   because the workflow failed or a receipt is absent.
3. If delivery is confirmed, do not resend. If non-delivery is verified and a
   resend is explicitly wanted, use the normal CLI in a configured environment:
   `uv run horizon --morning --morning-date YYYY-MM-DD`. Preserve its existing
   data directory and successful receipts. The workflow intentionally has no
   automatic override for ambiguous delivery.

Do not delete run history or clear receipts to force a retry. These protections
depend on retained Actions history and the cache; they are not a transactional
exactly-once email service. Manual CLI runs elsewhere are outside the workflow's
concurrency group. Keep the old local scheduler disabled to avoid double sends.

## Verification and remaining limit

Run `python -m unittest tests.test_morning tests.test_morning_schedule -v` for
offline boundary, history-pagination, rerun, cache-loss, ambiguity and fail-closed
tests. The dedicated PR workflow runs these without model/email secrets or live
delivery. The existing full application test suite is a separate check.

Changes take effect for scheduled runs only after being merged into the default
branch. Observe subsequent scheduled runs before claiming an improvement in
arrival time. A strict delivery-time requirement needs an independently reliable
scheduler/host and allowance for generation plus SMTP/inbox latency; that would
be a separate deployment decision, not a promise this cron patch can make.
