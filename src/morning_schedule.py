"""Fail-closed GitHub Actions guard for redundant morning triggers.

Uses only the standard library, so duplicate runs stop before dependency setup.
Run history is a second line of defence when the successful-delivery cache is
missing. It is deliberately conservative: entering the combined generate/send
step makes delivery uncertain, even if that step subsequently failed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .morning import morning_window


WORKFLOW_PATH = ".github/workflows/daily-summary.yml"
DELIVERY_STEP = "Generate and send morning email"


class UnsafeHistoryError(ValueError):
    """A fixed, credential-free diagnostic for unsafe or incomplete state."""


def github_get(path: str) -> dict:
    """Read trusted GitHub API paths; never log credentials or response bodies."""
    request = Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def pages(get, path: str, key: str, **query) -> list[dict]:
    """Exhaust pagination, or refuse to make a delivery decision."""
    result = []
    for page in range(1, 11):
        payload = get(path + "?" + urlencode({**query, "per_page": 100, "page": page}))
        values = payload[key]
        if not isinstance(values, list):
            raise UnsafeHistoryError("Invalid GitHub history response")
        total = payload["total_count"]
        if type(total) is not int or total < 0:
            raise UnsafeHistoryError("Invalid GitHub history count")
        result.extend(values)
        if len(values) < 100:
            if total != len(result):
                raise UnsafeHistoryError("Incomplete GitHub history response")
            return result
    raise UnsafeHistoryError("GitHub history exceeded the safe pagination limit")


def parse_timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise UnsafeHistoryError("GitHub timestamp has no timezone")
    return result


def issue_for_run(run: dict):
    # Pin before runner queueing. A run queued across UTC midnight must not
    # silently turn yesterday's issue into today's issue.
    return morning_window(now=parse_timestamp(run["created_at"]))


def delivery_attempted(job: dict) -> bool:
    for step in job["steps"]:
        if step["name"] != DELIVERY_STEP or step.get("conclusion") == "skipped":
            continue
        if step.get("started_at") or step.get("completed_at") or step["status"] in {"in_progress", "completed"}:
            return True
        # Live GitHub jobs report future steps as "pending", not only
        # "queued". Neither is a delivery attempt without timing/outcome
        # evidence. Do not generalize this to unknown or contradictory states.
        if step["status"] not in {"queued", "pending"} or step.get("conclusion") is not None:
            raise UnsafeHistoryError("Unknown or inconsistent delivery step state")
    return False


def delivery_blocker(get, repository: str, current: dict) -> str | None:
    """Return a reason to stop, including prior attempts of this same run."""
    window = issue_for_run(current)
    start = window.end.astimezone(timezone.utc)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    path = f"/repos/{repository}/actions/runs"
    runs = pages(
        get, path, "workflow_runs",
        created=f"{start.isoformat()}..{end.isoformat()}",
        branch=current["head_branch"],
    )
    # Do not rely on list consistency to discover previous attempts of self.
    runs = [run for run in runs if run["id"] != current["id"]] + [current]
    for run in runs:
        if run["workflow_id"] != current["workflow_id"] or run["head_branch"] != current["head_branch"]:
            continue
        if issue_for_run(run).end != window.end:
            continue
        if run["id"] != current["id"] and run["status"] == "in_progress":
            return f"Run {run['id']} is still active; delivery is not safe to repeat."
        # filter=all is essential: the default hides earlier attempts after a
        # rerun. Include self; this attempt's delivery step has not begun yet.
        jobs = pages(get, f"{path}/{run['id']}/jobs", "jobs", filter="all")
        if not jobs and (
            run["run_attempt"] > 1
            or (run["id"] != current["id"] and run["status"] == "completed"
                and run.get("conclusion") != "cancelled")
        ):
            raise UnsafeHistoryError("Prior job history is missing; delivery is unknown")
        if any(delivery_attempted(job) for job in jobs):
            return (
                f"Run {run['id']} already entered generation/delivery. "
                "Check that run and Gmail Sent before any manual recovery; "
                "no automatic resend will be attempted."
            )
    return None


def write_output(name: str, value: str) -> None:
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"{name}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["resolve", "check"])
    args = parser.parse_args()
    try:
        repository = os.environ["GITHUB_REPOSITORY"]
        current = github_get(f"/repos/{repository}/actions/runs/{int(os.environ['GITHUB_RUN_ID'])}")
        if current["path"].split("@", 1)[0] != WORKFLOW_PATH:
            raise UnsafeHistoryError("Unexpected workflow")
        window = issue_for_run(current)
        if args.command == "resolve":
            write_output("issue_date", window.metadata()["issue_date"])
            created = parse_timestamp(current["created_at"])
            delay = int((created - window.end).total_seconds())
            message = (
                f"Issue: {window.metadata()['issue_date']}; cutoff: {window.end.isoformat()}; "
                f"run created: {created.isoformat()}; {delay}s after cutoff "
                f"(event: {current['event']}). GitHub schedules are best-effort."
            )
            print(message)
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
                summary.write("### Morning timing\n\n" + message + "\n")
        else:
            blocker = delivery_blocker(github_get, repository, current)
            if blocker:
                print("::warning::" + blocker)
                write_output("send", "false")
            else:
                write_output("send", "true")
    except UnsafeHistoryError as error:
        # These messages are fixed literals from this module, never API data.
        raise SystemExit(
            f"Cannot establish safe morning delivery state ({error}); "
            "stopping without sending."
        ) from None
    except Exception as error:
        # Do not interpret a timeout, permission error or incomplete history as
        # permission to resend. Log only its class, not a potentially secret URL.
        raise SystemExit(
            f"Cannot establish safe morning delivery state ({type(error).__name__}); "
            "stopping without sending."
        ) from None


if __name__ == "__main__":
    main()
