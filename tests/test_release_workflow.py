from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
EXPECTED_WORKFLOW_SEMANTIC_SHA256 = (
    "824a396dc645223d7a308b263e64e9cf1574224327fd6cf7de12e46326ca6afd"
)

PINNED_ACTIONS = {
    "googleapis/release-please-action": "45996ed1f6d02564a971a2fa1b5860e934307cf7",
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "pypa/gh-action-pypi-publish": "dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
}
EXPECTED_JOBS = {
    "release-please",
    "stage-release",
    "publish-pypi",
    "publish-github-assets",
    "finalize-release",
}
RELEASE_NEEDED = "needs.stage-release.outputs.release_needed == 'true'"
DETECT_NEEDED = "steps.detect.outputs.release_needed == 'true'"
STAGE_NEEDED = DETECT_NEEDED + " || github.event_name == 'workflow_dispatch'"
PUSH_ONLY = "github.event_name == 'push'"
DISPATCH_ONLY = "github.event_name == 'workflow_dispatch'"
STAGE_GATE = (
    "always() && (github.event_name == 'workflow_dispatch' "
    "|| needs.release-please.result == 'success')"
)
FULL_SHA_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
MUTATING_RUN_PATTERNS = (
    re.compile(r"(^|\s)git\s+push(?:\s|$)"),
    re.compile(r"(^|\s)gh\s+(?:api|release)(?:\s|$)"),
    re.compile(r"(^|\s)curl(?:\.exe)?(?:\s|$)"),
    re.compile(r"(^|\s)(?:python\s+-m\s+)?twine\s+upload(?:\s|$)"),
)
EXPECTED_FINALIZE_COMMAND = (
    'python "$GUARD" finalize '
    '--repository "${{ github.repository }}" '
    '--tag "${{ needs.stage-release.outputs.tag_name }}" '
    '--source-sha "${{ needs.stage-release.outputs.source_sha }}" '
    '--expected-release-id "${{ needs.stage-release.outputs.release_id }}" '
    '--expected-manifest-sha256 "${{ needs.stage-release.outputs.manifest_sha256 }}" '
    '--expected-snapshot-sha256 "${{ needs.stage-release.outputs.snapshot_sha256 }}" '
    '--artifact-id "${{ needs.stage-release.outputs.artifact_id }}" '
    '--artifact-digest "${{ needs.stage-release.outputs.artifact_digest }}" '
    '--run-id "${{ needs.stage-release.outputs.run_id }}" '
    '--artifact-head-sha "${{ github.sha }}"'
)


def _load_workflow() -> dict[str, Any]:
    document = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(document, dict)
    return document


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    assert isinstance(steps, list) and all(isinstance(step, dict) for step in steps)
    return steps


def _needs(job: dict[str, Any]) -> list[str]:
    value = job.get("needs", [])
    return [value] if isinstance(value, str) else list(value)


def _canonical_shell(value: object) -> str:
    lines = [line.strip() for line in str(value or "").splitlines()]
    active = " ".join(line for line in lines if line and not line.startswith("#"))
    return re.sub(r"\s+", " ", active).strip()


def _single_action(job: dict[str, Any], repository: str) -> dict[str, Any]:
    matches = _matching_actions(job, repository)
    assert len(matches) == 1
    return matches[0]


def _matching_actions(job: dict[str, Any], repository: str) -> list[dict[str, Any]]:
    return [step for step in _steps(job) if str(step.get("uses", "")).startswith(f"{repository}@")]


def _first_action(job: dict[str, Any], repository: str) -> dict[str, Any]:
    """The job may check out a second ref for the dispatch guard path."""

    matches = _matching_actions(job, repository)
    assert 1 <= len(matches) <= 2
    return matches[0]


def _run_step(job: dict[str, Any], fragment: str) -> dict[str, Any]:
    matches = [step for step in _steps(job) if fragment in _canonical_shell(step.get("run"))]
    assert len(matches) == 1, fragment
    return matches[0]


def _sequence(job: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for step in _steps(job):
        if step.get("uses"):
            result.append(f"action:{str(step['uses']).split('@', 1)[0]}")
        else:
            command = _canonical_shell(step.get("run"))
            assert command
            result.append(f"run:{command.split(' ', 3)[0:3]}")
    return result


def assert_release_workflow_contract(document: dict[str, Any]) -> None:
    executable_semantics = copy.deepcopy(document)
    for job in executable_semantics.get("jobs", {}).values():
        for step in job.get("steps", []):
            if "run" in step:
                step["run"] = _canonical_shell(step["run"])
    semantic_bytes = json.dumps(
        executable_semantics,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert hashlib.sha256(semantic_bytes).hexdigest() == EXPECTED_WORKFLOW_SEMANTIC_SHA256
    assert set(document) == {"name", "on", "permissions", "jobs"}
    assert document["name"] == "Release"
    assert document["on"] == {
        "push": {"branches": ["main"]},
        "workflow_dispatch": {
            "inputs": {
                "tag_name": {
                    "description": "Existing release tag to publish, for example v0.4.1",
                    "required": "true",
                    "type": "string",
                },
                "guard_ref": {
                    "description": "Ref that provides tools/release_guard.py",
                    "required": "false",
                    "type": "string",
                    "default": "main",
                },
            }
        },
    }
    assert document["permissions"] == {"contents": "read"}
    jobs = document["jobs"]
    assert isinstance(jobs, dict) and set(jobs) == EXPECTED_JOBS

    release = jobs["release-please"]
    stage = jobs["stage-release"]
    pypi = jobs["publish-pypi"]
    assets = jobs["publish-github-assets"]
    finalize = jobs["finalize-release"]
    assert set(release) == {"if", "runs-on", "timeout-minutes", "permissions", "steps"}
    assert set(stage) == {
        "needs",
        "if",
        "runs-on",
        "timeout-minutes",
        "permissions",
        "outputs",
        "steps",
    }
    assert set(pypi) == {
        "needs",
        "if",
        "runs-on",
        "timeout-minutes",
        "environment",
        "permissions",
        "env",
        "steps",
    }
    for job in (assets, finalize):
        assert set(job) == {
            "needs",
            "if",
            "runs-on",
            "timeout-minutes",
            "permissions",
            "env",
            "steps",
        }
    assert _needs(stage) == ["release-please"]
    assert _needs(pypi) == ["stage-release"]
    assert _needs(assets) == ["stage-release", "publish-pypi"]
    assert _needs(finalize) == ["stage-release", "publish-pypi", "publish-github-assets"]
    assert pypi["if"] == assets["if"] == finalize["if"] == RELEASE_NEEDED

    assert release["permissions"] == {"contents": "write", "pull-requests": "write"}
    assert stage["permissions"] == {"contents": "write"}
    assert pypi["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    assert (
        assets["permissions"]
        == finalize["permissions"]
        == {
            "actions": "read",
            "contents": "write",
        }
    )
    assert pypi["environment"] == {
        "name": "pypi",
        "url": "https://pypi.org/p/dcc-mcp-material-maker",
    }
    for name, job in jobs.items():
        assert job["runs-on"] == "ubuntu-latest"
        assert job["timeout-minutes"] in {"10", "15"}
        if name != "publish-pypi":
            assert job.get("permissions", {}).get("id-token") != "write"

    expected_outputs = {
        "release_needed",
        "tag_name",
        "source_sha",
        "release_id",
        "manifest_sha256",
        "snapshot_sha256",
        "artifact_id",
        "artifact_digest",
        "run_id",
    }
    assert set(stage["outputs"]) == expected_outputs
    assert stage["outputs"]["artifact_id"] == "${{ steps.upload.outputs['artifact-id'] }}"
    assert stage["outputs"]["artifact_digest"] == "${{ steps.upload.outputs['artifact-digest'] }}"
    assert stage["outputs"]["run_id"] == "${{ github.run_id }}"

    allowed_job_ifs = {None, RELEASE_NEEDED, PUSH_ONLY, STAGE_GATE}
    allowed_step_ifs = {None, DETECT_NEEDED, PUSH_ONLY, DISPATCH_ONLY, STAGE_NEEDED}
    all_text: list[str] = []
    action_counts = {repository: 0 for repository in PINNED_ACTIONS}
    for job in jobs.values():
        assert job.get("if") in allowed_job_ifs
        for step in _steps(job):
            assert step.get("if") in allowed_step_ifs
            assert step.get("continue-on-error") is None
            uses = step.get("uses")
            if uses:
                assert set(step) <= {"id", "name", "if", "uses", "with"}
                assert FULL_SHA_ACTION.fullmatch(str(uses))
                repository, sha = str(uses).split("@", 1)
                assert PINNED_ACTIONS.get(repository) == sha
                action_counts[repository] += 1
            else:
                assert set(step) <= {"id", "name", "if", "run", "env"}
                command = _canonical_shell(step.get("run"))
                mutating_guard = '"$GUARD"' in command and any(
                    word in command
                    for word in (" stage ", "verify-artifact", "publish-assets", " finalize ")
                )
                expected_env = None
                if mutating_guard:
                    expected_env = {"GH_TOKEN": "${{ github.token }}"}
                    if " stage " in command:
                        expected_env["GUARD"] = (
                            "${{ github.event_name == 'workflow_dispatch'"
                            " && format('{0}/release-guard/release_guard.py', runner.temp)"
                            " || 'tools/release_guard.py' }}"
                        )
                        expected_env["RELEASE_TAG"] = (
                            "${{ github.event_name == 'workflow_dispatch' && inputs.tag_name"
                            " || steps.detect.outputs.tag_name }}"
                        )
                        expected_env["RELEASE_SHA"] = "${{ steps.resolve.outputs.source_sha }}"
                assert step.get("env") == expected_env
                for pattern in MUTATING_RUN_PATTERNS:
                    assert pattern.search(command) is None
                all_text.append(command)
    assert action_counts == {
        "googleapis/release-please-action": 1,
        "actions/checkout": 8,
        "actions/setup-python": 4,
        "actions/upload-artifact": 1,
        "actions/download-artifact": 3,
        "pypa/gh-action-pypi-publish": 1,
    }
    serialized = "\n".join(all_text) + str(document)
    assert "${{ secrets." not in serialized

    release_action = _single_action(release, "googleapis/release-please-action")
    assert release_action["with"] == {
        "token": "${{ github.token }}",
        "config-file": "release-please-config.json",
        "manifest-file": ".release-please-manifest.json",
    }
    stage_checkout = _steps(stage)[0]
    assert str(stage_checkout.get("uses", "")).startswith("actions/checkout@")
    assert stage_checkout["with"] == {
        "ref": "${{ github.event_name == 'workflow_dispatch' && inputs.tag_name || github.sha }}",
        "fetch-depth": "${{ github.event_name == 'workflow_dispatch' && '1' || '2' }}",
        "persist-credentials": "false",
    }
    guard_checkout = _steps(stage)[1]
    assert str(guard_checkout.get("uses", "")).startswith("actions/checkout@")
    assert guard_checkout["if"] == DISPATCH_ONLY
    assert guard_checkout["with"]["path"] == ".release-guard"
    assert guard_checkout["with"]["ref"] == "${{ inputs.guard_ref }}"
    assert (
        _steps(stage).index(_run_step(stage, " detect-release "))
        < _steps(stage).index(_run_step(stage, " -m build"))
        < _steps(stage).index(_run_step(stage, " stage --repository "))
    )

    upload = _single_action(stage, "actions/upload-artifact")
    assert upload["with"] == {
        "name": "release-distributions",
        "path": "dist/\nrelease/\n",
        "if-no-files-found": "error",
        "compression-level": "0",
        "retention-days": "1",
        "overwrite": "false",
    }
    for job in (pypi, assets, finalize):
        checkout = _steps(job)[0]
        assert str(checkout.get("uses", "")).startswith("actions/checkout@")
        guard_checkout = _steps(job)[1]
        assert str(guard_checkout.get("uses", "")).startswith("actions/checkout@")
        assert guard_checkout["if"] == DISPATCH_ONLY
        assert guard_checkout["with"]["path"] == ".release-guard"
        assert job["env"]["GUARD"] == (
            "${{ github.event_name == 'workflow_dispatch' && "
            "'.release-guard/tools/release_guard.py' || 'tools/release_guard.py' }}"
        )
        assert checkout["with"] == {
            "ref": "${{ needs.stage-release.outputs.source_sha }}",
            "fetch-depth": "1",
            "persist-credentials": "false",
        }
        download = _single_action(job, "actions/download-artifact")
        # Without merge-multiple, download-artifact v4 extracts into
        # ./<artifact-name>/ instead of the workspace root, so the guard can
        # no longer find dist/ or release/.
        assert download["with"] == {
            "artifact-ids": "${{ needs.stage-release.outputs.artifact_id }}",
            "path": ".",
            "merge-multiple": "true",
        }
        assert _steps(job).index(_run_step(job, " verify-artifact ")) < _steps(job).index(download)

    publisher = _single_action(pypi, "pypa/gh-action-pypi-publish")
    assert publisher["with"] == {
        "packages-dir": "dist",
        "skip-existing": "true",
        "verbose": "true",
        "print-hash": "true",
        "attestations": "true",
    }
    assert _steps(pypi).index(_run_step(pypi, " pypi-preflight ")) + 1 == _steps(pypi).index(
        publisher
    )
    assert _steps(pypi).index(_run_step(pypi, " pypi-verify ")) == _steps(pypi).index(publisher) + 1
    assert _run_step(assets, " publish-assets ")
    finalize_step = _run_step(finalize, " finalize ")
    assert _steps(finalize)[-1] is finalize_step
    assert finalize_step["name"] == "Reverify exact PyPI state and publish release"
    assert _canonical_shell(finalize_step["run"]) == EXPECTED_FINALIZE_COMMAND


def test_release_workflow_is_semantically_fail_closed() -> None:
    assert_release_workflow_contract(_load_workflow())


def test_contract_ignores_comment_decoys_but_rejects_extra_mutation_paths() -> None:
    document = _load_workflow()
    stage = _run_step(document["jobs"]["stage-release"], " stage --repository ")
    stage["run"] = "# gh api /decoy\n" + stage["run"]
    assert_release_workflow_contract(document)

    mutated = copy.deepcopy(document)
    mutated["jobs"]["stage-release"]["steps"].append(
        {"name": "hidden publisher", "run": "python -m twine upload dist/*"}
    )
    with pytest.raises(AssertionError):
        assert_release_workflow_contract(mutated)


def test_contract_rejects_allowlisted_action_at_untrusted_sha() -> None:
    document = _load_workflow()
    checkout = _first_action(document["jobs"]["stage-release"], "actions/checkout")
    checkout["uses"] = "actions/checkout@" + "f" * 40
    with pytest.raises(AssertionError):
        assert_release_workflow_contract(document)


@pytest.mark.parametrize(
    "case",
    [
        "always",
        "git-push",
        "extra-secret",
        "repository-drift",
        "persisted-credentials",
        "extra-credentialed-checkout",
        "extra-job-token",
        "reordered-actions",
        "duplicate-finalize",
    ],
)
def test_contract_rejects_alternate_mutation_and_credential_routes(case: str) -> None:
    document = _load_workflow()
    jobs = document["jobs"]
    stage = _run_step(jobs["stage-release"], " stage --repository ")
    checkout = _first_action(jobs["stage-release"], "actions/checkout")
    if case == "always":
        _single_action(jobs["publish-pypi"], "pypa/gh-action-pypi-publish")["if"] = "always()"
    elif case == "git-push":
        stage["run"] += "\ngit push origin HEAD:main"
    elif case == "extra-secret":
        stage["env"]["EXTRA_TOKEN"] = "${{ secrets.EXTRA_TOKEN }}"
    elif case == "repository-drift":
        checkout["with"]["repository"] = "other/project"
    elif case == "persisted-credentials":
        checkout["with"]["persist-credentials"] = "true"
    elif case == "extra-credentialed-checkout":
        jobs["release-please"]["steps"].insert(
            0, {"uses": f"actions/checkout@{PINNED_ACTIONS['actions/checkout']}"}
        )
    elif case == "extra-job-token":
        jobs["stage-release"]["env"] = {"GH_TOKEN": "${{ github.token }}"}
    elif case == "reordered-actions":
        steps = jobs["stage-release"]["steps"]
        steps[0], steps[1] = steps[1], steps[0]
    elif case == "duplicate-finalize":
        jobs["finalize-release"]["steps"].append(
            copy.deepcopy(_run_step(jobs["finalize-release"], " finalize "))
        )
    else:  # pragma: no cover
        raise AssertionError(case)
    with pytest.raises(AssertionError):
        assert_release_workflow_contract(document)


@pytest.mark.parametrize(
    "case",
    [
        "earlier-only-verifier",
        "later-decoy",
        "warning-only",
        "different-project",
        "different-version",
        "existence-only",
    ],
)
def test_contract_rejects_finalize_pypi_recheck_mutants(case: str) -> None:
    document = _load_workflow()
    finalize = document["jobs"]["finalize-release"]
    step = _run_step(finalize, " finalize ")
    if case == "earlier-only-verifier":
        finalize["steps"].insert(
            -1,
            {
                "name": "earlier verifier",
                "run": 'python "$GUARD" pypi-verify --source-sha earlier',
            },
        )
        step["run"] = "echo finalize"
    elif case == "later-decoy":
        finalize["steps"].append({"name": "later decoy", "run": "echo finalize"})
    elif case == "warning-only":
        step["run"] += " || echo warning"
    elif case == "different-project":
        step["run"] += " --project other-project"
    elif case == "different-version":
        step["run"] += " --version 9.9.9"
    elif case == "existence-only":
        step["run"] = step["run"].replace(" finalize ", " pypi-preflight ") + " && echo finalize"
    else:  # pragma: no cover
        raise AssertionError(case)

    with pytest.raises(AssertionError):
        assert_release_workflow_contract(document)


def _hidden_python_publisher() -> dict[str, str]:
    return {
        "name": "hidden Python publisher",
        "run": (
            'python -c "import urllib.request; '
            "print('${{ github.token }}', urllib.request.Request)\""
        ),
    }


@pytest.mark.parametrize("job_name", sorted(EXPECTED_JOBS))
def test_contract_rejects_arbitrary_executable_step_at_every_job_boundary(job_name: str) -> None:
    baseline = _load_workflow()
    step_count = len(_steps(baseline["jobs"][job_name]))
    for insertion_index in range(step_count + 1):
        document = copy.deepcopy(baseline)
        _steps(document["jobs"][job_name]).insert(insertion_index, _hidden_python_publisher())
        with pytest.raises(AssertionError):
            assert_release_workflow_contract(document)


def test_contract_rejects_hidden_publisher_appended_to_every_run_step() -> None:
    baseline = _load_workflow()
    mutations: list[tuple[str, int]] = []
    for job_name, job in baseline["jobs"].items():
        for index, step in enumerate(_steps(job)):
            if "run" in step:
                mutations.append((job_name, index))

    for job_name, index in mutations:
        document = copy.deepcopy(baseline)
        step = _steps(document["jobs"][job_name])[index]
        step["run"] += "\n" + _hidden_python_publisher()["run"]
        with pytest.raises(AssertionError):
            assert_release_workflow_contract(document)


def test_workflow_binds_exact_artifact_and_release_identity_for_every_consumer() -> None:
    jobs = _load_workflow()["jobs"]
    for name in ("publish-pypi", "publish-github-assets", "finalize-release"):
        text = "\n".join(_canonical_shell(step.get("run")) for step in _steps(jobs[name]))
        for binding in ("artifact_id", "artifact_digest", "source_sha", "run_id"):
            assert f"needs.stage-release.outputs.{binding}" in text
    for name in ("publish-github-assets", "finalize-release"):
        text = "\n".join(_canonical_shell(step.get("run")) for step in _steps(jobs[name]))
        for binding in (
            "tag_name",
            "source_sha",
            "release_id",
            "manifest_sha256",
            "snapshot_sha256",
            "artifact_id",
            "artifact_digest",
            "run_id",
        ):
            assert f"needs.stage-release.outputs.{binding}" in text


def test_dispatch_path_builds_from_the_tag_and_runs_the_reviewed_guard() -> None:
    """A backfill must build the tag, not the maintenance ref it runs on."""

    jobs = _load_workflow()["jobs"]
    stage = jobs["stage-release"]

    checkout, guard_checkout = _matching_actions(stage, "actions/checkout")
    assert checkout["with"]["ref"] == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.tag_name || github.sha }}"
    )
    assert guard_checkout["if"] == DISPATCH_ONLY
    assert guard_checkout["with"]["ref"] == "${{ inputs.guard_ref }}"
    assert guard_checkout["with"]["path"] == ".release-guard"

    stage_step = _run_step(stage, " stage --repository ")
    assert stage_step["env"]["GUARD"] == (
        "${{ github.event_name == 'workflow_dispatch'"
        " && format('{0}/release-guard/release_guard.py', runner.temp)"
        " || 'tools/release_guard.py' }}"
    )
    assert stage_step["env"]["RELEASE_TAG"] == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.tag_name"
        " || steps.detect.outputs.tag_name }}"
    )
    assert stage_step["env"]["RELEASE_SHA"] == "${{ steps.resolve.outputs.source_sha }}"

    commands = [_canonical_shell(step.get("run")) for step in _steps(stage)]
    joined = "\n".join(commands)
    # The guard is staged outside the tree so the sdist cannot absorb it.
    assert "mv .release-guard/tools/release_guard.py" in joined
    assert "rm -rf .release-guard" in joined


def test_dispatch_path_keeps_the_push_gate_and_publish_gate_intact() -> None:
    jobs = _load_workflow()["jobs"]

    # release-please only detects a version transition on a push to main.
    assert jobs["release-please"]["if"] == PUSH_ONLY
    assert jobs["stage-release"]["if"] == STAGE_GATE
    # stage-release still refuses to run when release-please failed on push.
    assert "needs.release-please.result == 'success'" in jobs["stage-release"]["if"]
    for name in ("publish-pypi", "publish-github-assets", "finalize-release"):
        assert jobs[name]["if"] == RELEASE_NEEDED
    assert jobs["stage-release"]["outputs"]["release_needed"] == (
        "${{ steps.detect.outputs.release_needed == 'true'"
        " || github.event_name == 'workflow_dispatch' }}"
    )


def test_every_consumer_binds_the_run_head_for_the_dispatch_path() -> None:
    """A dispatched run builds the tag but runs on main, so it declares the head."""

    jobs = _load_workflow()["jobs"]
    for name in ("publish-pypi", "publish-github-assets", "finalize-release"):
        commands = "\n".join(_canonical_shell(step.get("run")) for step in _steps(jobs[name]))
        assert '--artifact-head-sha "${{ github.sha }}"' in commands
