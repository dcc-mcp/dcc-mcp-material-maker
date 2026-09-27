"""Contract tests for the manual backfill release channel.

The backfill workflow replays the release chain for a tag whose publication
never finished. It must stay as fail-closed as ``release.yml``: every external
mutation goes through ``tools/release_guard.py``, every job binds the exact
artifact and release identity, and only the PyPI job may mint an OIDC token.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "backfill-release.yml"

EXPECTED_JOBS = {
    "stage-release",
    "publish-pypi",
    "publish-github-assets",
    "finalize-release",
}
FULL_SHA_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
MUTATING_RUN_PATTERNS = (
    re.compile(r"(^|\s)git\s+push(?:\s|$)"),
    re.compile(r"(^|\s)gh\s+(?:api|release)(?:\s|$)"),
    re.compile(r"(^|\s)curl(?:\.exe)?(?:\s|$)"),
    re.compile(r"(^|\s)(?:python\s+-m\s+)?twine\s+upload(?:\s|$)"),
)
GUARD = "release_guard.py"
REQUIRED_BINDINGS = (
    "--artifact-id",
    "--artifact-digest",
    "--run-id",
    "--expected-release-id",
    "--expected-manifest-sha256",
    "--expected-snapshot-sha256",
)


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    loaded = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    assert isinstance(steps, list) and all(isinstance(step, dict) for step in steps)
    return steps


def _needs(job: dict[str, Any]) -> list[str]:
    value = job.get("needs", [])
    return [value] if isinstance(value, str) else list(value)


def _shell(value: object) -> str:
    lines = [line.strip() for line in str(value or "").splitlines()]
    active = " ".join(line for line in lines if line and not line.startswith("#"))
    return re.sub(r"\s+", " ", active).strip()


def _unquoted(value: object) -> str:
    return _shell(value).replace('"', "")


def test_backfill_is_dispatch_only_and_requires_an_exact_tag(document) -> None:
    assert document["name"] == "Backfill release"
    assert list(document["on"]) == ["workflow_dispatch"]
    inputs = document["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"tag_name", "guard_ref"}
    assert inputs["tag_name"]["required"] == "true"
    assert inputs["guard_ref"]["default"] == "main"
    assert document["permissions"] == {"contents": "read"}


def test_backfill_jobs_replay_the_release_chain_in_order(document) -> None:
    jobs = document["jobs"]
    assert set(jobs) == EXPECTED_JOBS
    assert _needs(jobs["publish-pypi"]) == ["stage-release"]
    assert _needs(jobs["publish-github-assets"]) == ["stage-release", "publish-pypi"]
    assert _needs(jobs["finalize-release"]) == [
        "stage-release",
        "publish-pypi",
        "publish-github-assets",
    ]
    for job in jobs.values():
        assert job["runs-on"] == "ubuntu-latest"
        assert job["timeout-minutes"] in {"10", "15"}


def test_backfill_uses_only_pinned_actions_and_guarded_mutations(document) -> None:
    for name, job in document["jobs"].items():
        for step in _steps(job):
            if "uses" in step:
                assert FULL_SHA_ACTION.match(str(step["uses"])), f"{name}: {step['uses']}"
                continue
            command = _unquoted(step.get("run"))
            assert command
            for pattern in MUTATING_RUN_PATTERNS:
                assert not pattern.search(command), f"{name}: {command}"


def test_backfill_only_the_pypi_job_mints_an_oidc_token(document) -> None:
    jobs = document["jobs"]
    assert jobs["publish-pypi"]["permissions"]["id-token"] == "write"
    assert jobs["publish-pypi"]["environment"]["name"] == "pypi"
    for name in ("stage-release", "publish-github-assets", "finalize-release"):
        assert jobs[name]["permissions"].get("id-token") != "write"
    assert jobs["stage-release"]["permissions"] == {"contents": "write"}
    assert jobs["publish-pypi"]["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    assert jobs["publish-github-assets"]["permissions"] == {
        "actions": "read",
        "contents": "write",
    }
    assert jobs["finalize-release"]["permissions"] == {
        "actions": "read",
        "contents": "write",
    }


def test_backfill_builds_from_the_tag_and_runs_the_reviewed_guard(document) -> None:
    steps = _steps(document["jobs"]["stage-release"])
    checkouts = [step for step in steps if str(step.get("uses", "")).startswith("actions/checkout")]
    assert len(checkouts) == 2
    assert checkouts[0]["with"]["ref"] == "${{ inputs.tag_name }}"
    assert checkouts[1]["with"]["ref"] == "${{ inputs.guard_ref }}"
    assert checkouts[1]["with"]["path"] == ".release-guard"

    commands = [_unquoted(step.get("run")) for step in steps]
    joined = "\n".join(commands)
    assert GUARD in joined
    assert "python -m build" in joined
    # The guard is staged outside the tree so the sdist cannot absorb it.
    assert "mv .release-guard/tools/release_guard.py" in joined
    assert "rm -rf .release-guard" in joined
    stage = [command for command in commands if f"{GUARD} stage" in command]
    assert len(stage) == 1
    assert "RUNNER_TEMP" in stage[0]
    assert "--tag ${{ inputs.tag_name }}" in stage[0]


def test_backfill_every_consumer_binds_exact_artifact_and_release_identity(document) -> None:
    outputs = document["jobs"]["stage-release"]["outputs"]
    assert set(outputs) == {
        "tag_name",
        "source_sha",
        "release_id",
        "manifest_sha256",
        "snapshot_sha256",
        "artifact_id",
        "artifact_digest",
        "run_id",
    }
    for name in ("publish-pypi", "publish-github-assets", "finalize-release"):
        commands = "\n".join(_unquoted(step.get("run")) for step in _steps(document["jobs"][name]))
        assert "verify-artifact" in commands
        assert "--artifact-id ${{ needs.stage-release.outputs.artifact_id }}" in commands
        assert "--artifact-digest ${{ needs.stage-release.outputs.artifact_digest }}" in commands
        assert "--run-id ${{ needs.stage-release.outputs.run_id }}" in commands
    for name in ("publish-github-assets", "finalize-release"):
        commands = "\n".join(_unquoted(step.get("run")) for step in _steps(document["jobs"][name]))
        for binding in REQUIRED_BINDINGS:
            assert binding in commands, f"{name} is missing {binding}"


def test_backfill_step_order_stages_then_verifies_then_publishes_last(document) -> None:
    jobs = document["jobs"]

    def sequence(name: str) -> list[str]:
        result = []
        for step in _steps(jobs[name]):
            if "uses" in step:
                result.append(f"action:{str(step['uses']).split('@', 1)[0]}")
                continue
            command = _unquoted(step.get("run"))
            for fragment in (
                "release_guard.py stage",
                "release_guard.py verify-artifact",
                "release_guard.py pypi-preflight",
                "release_guard.py pypi-verify",
                "release_guard.py publish-assets",
                "release_guard.py finalize",
                "python -m build",
                "python -m twine check",
                "git rev-parse",
            ):
                if fragment in command:
                    result.append(fragment)
                    break
            else:
                result.append("run:setup")
        return result

    assert sequence("stage-release") == [
        "action:actions/checkout",
        "action:actions/checkout",
        "run:setup",
        "git rev-parse",
        "action:actions/setup-python",
        "run:setup",
        "python -m build",
        "python -m twine check",
        "release_guard.py stage",
        "action:actions/upload-artifact",
    ]
    assert sequence("publish-pypi") == [
        "action:actions/checkout",
        "action:actions/checkout",
        "action:actions/setup-python",
        "release_guard.py verify-artifact",
        "action:actions/download-artifact",
        "release_guard.py pypi-preflight",
        "action:pypa/gh-action-pypi-publish",
        "release_guard.py pypi-verify",
    ]
    assert sequence("publish-github-assets")[-1] == "release_guard.py publish-assets"
    assert sequence("finalize-release")[-1] == "release_guard.py finalize"
