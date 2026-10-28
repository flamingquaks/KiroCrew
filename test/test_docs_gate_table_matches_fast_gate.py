from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FAST_GATE = ROOT / ".github" / "workflows" / "fast-gate.yml"
GATE_DOC = ROOT / "docs" / "ci" / "ci-and-reviews.md"

_JOB_RE = re.compile(r"^  ([a-z0-9][a-z0-9-]*):\s*$")
_DOC_ROW_RE = re.compile(r"^\|\s*`([a-z0-9][a-z0-9-]*)`")


def fast_gate_jobs() -> set[str]:
    text = FAST_GATE.read_text(encoding="utf-8").splitlines()
    jobs: set[str] = set()
    in_jobs = False
    for line in text:
        if not in_jobs:
            if line == "jobs:":
                in_jobs = True
            continue
        match = _JOB_RE.match(line)
        if match:
            jobs.add(match.group(1))
    return jobs


def documented_gate_jobs() -> set[str]:
    return _documented_rows("| Job |")


def documented_gate_steps() -> set[str]:
    return _documented_rows("| Gate step |")


def _documented_rows(header_prefix: str) -> set[str]:
    text = GATE_DOC.read_text(encoding="utf-8").splitlines()
    rows: set[str] = set()
    in_section = False
    in_table = False
    for line in text:
        if line.startswith("## `fast-gate.yml`"):
            in_section = True
            continue
        if in_section and not in_table:
            if line.startswith(header_prefix):
                in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            match = _DOC_ROW_RE.match(line)
            if match and match.group(1) not in ("Job", "Gate"):
                rows.add(match.group(1))
    return rows


# Each gate step is documented under the short name of the script it runs; the
# step itself is identified by that script in the workflow.
_STEP_DOC_NAMES = {
    "scripts/verify_vendor_manifest.py": "vendor-manifest",
    "scripts/check_brand_name.py": "brand-lint",
    "scripts/check_comment_history.py": "comment-history-lint",
    "scripts/check_focus_cue.py": "focus-cue-lint",
    "scripts/check_feature_map.py": "feature-map-lint",
    "scripts/check_changelog_history.py": "changelog-history",
    "scripts/check_decisions_history.py": "decision-ledger-history",
    "scripts/check_builtin_skill_scope.py": "builtin-skill-scope",
    "scripts/check_loop_bound_locks.py": "loop-bound-locks",
    "scripts/check_testpaths_coverage.py": "testpaths-coverage",
    "scripts/check_cwd_relative_repo_reads.py": "cwd-relative-repo-reads",
    "scripts/check_harness_parity.py": "harness-parity",
    "scripts/check_memory_store_seam.py": "memory-store-seam",
    "scripts/docs-lint.sh": "docs-lint",
}


def fast_gate_source_gate_steps() -> set[str]:
    workflow = yaml.safe_load(FAST_GATE.read_text(encoding="utf-8"))
    runs = [
        str(step.get("run", ""))
        for step in workflow["jobs"]["source-gates"]["steps"]
        if "run" in step
    ]
    return {name for script, name in _STEP_DOC_NAMES.items() if any(script in r for r in runs)}


class TestFastGateDocParity:
    def test_comment_history_gate_is_documented(self) -> None:
        assert "comment-history-lint" in fast_gate_source_gate_steps()
        assert "comment-history-lint" in documented_gate_steps()

    def test_doc_table_matches_workflow_jobs(self) -> None:
        workflow = fast_gate_jobs()
        documented = documented_gate_jobs()
        assert documented == workflow, (
            "docs table drift: workflow-only="
            f"{sorted(workflow - documented)} doc-only={sorted(documented - workflow)}"
        )

    def test_step_table_matches_the_gates_source_gates_runs(self) -> None:
        # The job table says there are fourteen gates; this is the table that
        # names them, so a gate added to or dropped from the job must move a
        # row here too.
        workflow = fast_gate_source_gate_steps()
        documented = documented_gate_steps()
        assert documented == workflow, (
            "gate-step table drift: workflow-only="
            f"{sorted(workflow - documented)} doc-only={sorted(documented - workflow)}"
        )
        assert documented == set(_STEP_DOC_NAMES.values())
