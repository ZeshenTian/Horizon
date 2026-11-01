"""Offline safety checks: python -m unittest tests.test_morning_schedule."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from src import morning_schedule as schedule


def run(run_id=10, **overrides):
    return {
        "id": run_id, "workflow_id": 123, "path": schedule.WORKFLOW_PATH,
        "head_branch": "main", "created_at": "2026-11-01T00:07:00Z",
        "status": "completed", "event": "schedule", "run_attempt": 1,
        **overrides,
    }


def job(status="completed", conclusion="success", **overrides):
    return {"steps": [{
        "name": schedule.DELIVERY_STEP, "status": status,
        "conclusion": conclusion, **overrides,
    }]}


class History:
    def __init__(self, runs=(), jobs=None):
        self.runs = list(runs)
        self.jobs = jobs or {}
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/jobs"):
            assert query["filter"] == ["all"]
            key = "jobs"
            values = self.jobs.get(int(parsed.path.split("/")[-2]), [])
        else:
            assert query["branch"] == ["main"]
            assert "2026-11-01T00:00:00" in query["created"][0]
            key, values = "workflow_runs", self.runs
        page = int(query["page"][0])
        return {key: values[(page - 1) * 100:page * 100], "total_count": len(values)}


class ScheduleTests(unittest.TestCase):
    def test_first_trigger_can_send(self):
        self.assertIsNone(schedule.delivery_blocker(History(), "owner/repo", run()))

    def test_cached_receipt_loss_still_blocks_previous_delivery(self):
        history = History([run(1)], {1: [job()]})
        self.assertIn("Run 1", schedule.delivery_blocker(history, "owner/repo", run()))

    def test_failed_cancelled_or_running_delivery_is_not_safe_to_repeat(self):
        for status, conclusion in [("completed", "failure"), ("completed", "cancelled"), ("in_progress", None)]:
            with self.subTest(conclusion=conclusion):
                history = History([run(1)], {1: [job(status, conclusion)]})
                self.assertIsNotNone(schedule.delivery_blocker(history, "owner/repo", run()))

    def test_setup_failure_with_skipped_delivery_can_retry(self):
        history = History([run(1)], {1: [job(conclusion="skipped", started_at="2026-11-01T00:01:00Z")]})
        self.assertIsNone(schedule.delivery_blocker(history, "owner/repo", run()))

    def test_newer_run_can_have_delivered_before_older_run_gets_runner(self):
        history = History([run(20, created_at="2026-11-01T00:27:00Z")], {20: [job()]})
        self.assertIsNotNone(schedule.delivery_blocker(history, "owner/repo", run()))

    def test_same_run_rerun_checks_all_prior_attempts_even_if_missing_from_list(self):
        history = History([], {10: [job(), job("queued", None)]})
        self.assertIsNotNone(schedule.delivery_blocker(history, "owner/repo", run(run_attempt=2)))

    def test_other_run_rerun_does_not_hide_earlier_attempt(self):
        history = History([run(1, run_attempt=2)], {1: [job(), job(conclusion="skipped")]})
        self.assertIsNotNone(schedule.delivery_blocker(history, "owner/repo", run()))

    def test_current_attempt_future_delivery_is_not_a_blocker(self):
        history = History([run()], {10: [job("queued", None)]})
        self.assertIsNone(schedule.delivery_blocker(history, "owner/repo", run(status="in_progress")))

    def test_other_active_run_blocks_even_before_delivery(self):
        history = History([run(1, status="in_progress")])
        self.assertIn("still active", schedule.delivery_blocker(history, "owner/repo", run()))

    def test_other_dates_branches_and_workflows_are_not_delivery_evidence(self):
        history = History([
            run(1, created_at="2026-10-31T23:59:00Z"),
            run(2, head_branch="feature"), run(3, workflow_id=456),
        ], {1: [job()], 2: [job()], 3: [job()]})
        self.assertIsNone(schedule.delivery_blocker(history, "owner/repo", run()))

    def test_queue_crossing_midnight_keeps_original_issue(self):
        current = run(created_at="2026-12-31T23:59:00Z", run_started_at="2027-01-01T00:01:00Z")
        self.assertEqual(schedule.issue_for_run(current).end, datetime(2026, 12, 31, tzinfo=timezone.utc))

    def test_runs_and_jobs_paginate(self):
        history = History([run(i, workflow_id=456) for i in range(100)] + [run(101)],
                          {101: [{"steps": []}] * 100 + [job()]})
        self.assertIsNotNone(schedule.delivery_blocker(history, "owner/repo", run()))
        self.assertEqual(sum("page=2" in url for url in history.calls), 2)

    def test_missing_or_truncated_history_fails_closed(self):
        for response in [{}, {"workflow_runs": []}, {"workflow_runs": [], "total_count": 1}, {"workflow_runs": [], "total_count": -1}, {"workflow_runs": "wrong"}]:
            with self.subTest(response=response), self.assertRaises((ValueError, KeyError)):
                schedule.delivery_blocker(lambda _: response, "owner/repo", run())

    def test_missing_previous_job_history_fails_closed(self):
        for history, current in [
            (History([run(1)]), run()),
            (History(), run(run_attempt=2)),
        ]:
            with self.subTest(current=current), self.assertRaises(ValueError):
                schedule.delivery_blocker(history, "owner/repo", current)

    def test_cancelled_before_start_and_queued_runs_have_no_delivery(self):
        history = History([run(1, conclusion="cancelled"), run(2, status="queued")])
        self.assertIsNone(schedule.delivery_blocker(history, "owner/repo", run()))

    def test_api_error_fails_closed(self):
        with self.assertRaises(TimeoutError):
            schedule.delivery_blocker(Mock(side_effect=TimeoutError), "owner/repo", run())

    def test_unknown_step_state_fails_closed(self):
        with self.assertRaises(ValueError):
            schedule.delivery_attempted(job("unknown", None))

    def test_cli_resolves_original_issue_and_reports_delay(self):
        with tempfile.TemporaryDirectory() as directory:
            output, summary = Path(directory) / "output", Path(directory) / "summary"
            env = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "10", "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
            with patch.dict(os.environ, env), patch("sys.argv", ["schedule", "resolve"]), patch.object(schedule, "github_get", return_value=run()), redirect_stdout(StringIO()):
                schedule.main()
            self.assertEqual(output.read_text(), "issue_date=2026-11-01\n")
            self.assertIn("420s after cutoff", summary.read_text())

    def test_cli_sets_explicit_send_decision(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output"
                env = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "10", "GITHUB_OUTPUT": str(output)}
                history = History([run(1)] if blocked else [], {1: [job()]})
                def get(url):
                    return run() if url.endswith("/runs/10") else history(url)
                with patch.dict(os.environ, env), patch("sys.argv", ["schedule", "check"]), patch.object(schedule, "github_get", side_effect=get), redirect_stdout(StringIO()):
                    schedule.main()
                self.assertEqual(output.read_text(), "send=" + ("false" if blocked else "true") + "\n")

    def test_cli_failure_does_not_enable_send_or_expose_error_details(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            env = {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "10", "GITHUB_OUTPUT": str(output)}
            with patch.dict(os.environ, env), patch("sys.argv", ["schedule", "check"]), patch.object(schedule, "github_get", side_effect=RuntimeError("sensitive detail")):
                with self.assertRaises(SystemExit) as error:
                    schedule.main()
            self.assertNotIn("sensitive detail", str(error.exception))
            self.assertFalse(output.exists())

    def test_workflow_wires_guards_before_dependencies_and_keeps_cutoff(self):
        workflow = (Path(__file__).resolve().parents[1] / schedule.WORKFLOW_PATH).read_text()
        self.assertIn("cron: '0 0 * * *'", workflow)
        self.assertIn("cron: '7,27,47 0 * * *'", workflow)
        self.assertIn("cron: '17 1 * * *'", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("github.event.repository.default_branch", workflow)
        self.assertIn("actions: read", workflow)
        self.assertLess(workflow.index("id: receipt"), workflow.index("Install dependencies"))
        self.assertLess(workflow.index("id: guard"), workflow.index("Install dependencies"))
        self.assertEqual(workflow.count("if: steps.guard.outputs.send == 'true'"), 5)
        self.assertIn("--morning-date \"$ISSUE_DATE\"", workflow)
        self.assertIn("- name: " + schedule.DELIVERY_STEP, workflow)


if __name__ == "__main__":
    unittest.main()
