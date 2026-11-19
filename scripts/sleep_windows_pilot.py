"""Opt-in SessionEnd marker and disposable, offline SkillOpt-Sleep pilot."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

UPSTREAM = "22c1ed85d20bf09476373c4a1b10965284873e3d"
HOOK_INPUT_LIMIT = 64 * 1024
SKILL_FILE = Path(".claude/skills/pilot/SKILL.md")
MEMORY_FILE = Path("CLAUDE.md")


class PilotError(ValueError):
    pass


def mark_session(marker_dir: Path) -> None:
    if not marker_dir.is_absolute():
        raise PilotError("--marker-dir must be an absolute path")
    raw = sys.stdin.buffer.read(HOOK_INPUT_LIMIT + 1)
    if len(raw) > HOOK_INPUT_LIMIT:
        raise PilotError("SessionEnd input is too large")
    try:
        event = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PilotError("SessionEnd input must be a JSON object") from exc
    if not isinstance(event, dict) or event.get("hook_event_name") != "SessionEnd":
        raise PilotError("expected a SessionEnd event")
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise PilotError("SessionEnd input has no session_id")
    try:
        session_sha = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise PilotError("invalid SessionEnd session_id") from exc

    marker = {
        "event": "SessionEnd",
        "session_sha256": session_sha,
        "utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    marker_dir.mkdir(parents=True, exist_ok=True)
    # Publish one complete record per delivery. Close the file before rename on Windows.
    handle, name = tempfile.mkstemp(dir=marker_dir, prefix="end-", suffix=".tmp")
    pending = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write((json.dumps(marker) + "\n").encode("utf-8"))
        pending.replace(pending.with_suffix(".json"))
    finally:
        pending.unlink(missing_ok=True)


def upstream_zip(repo: Path) -> bytes:
    # Use Git objects, independent of checkout contents, and forbid lazy network fetches.
    git_env = dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="", GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(repo.resolve()), "archive", "--format=zip", UPSTREAM, "skillopt_sleep"],
        env=git_env, capture_output=True, timeout=20, check=False,
    )
    if result.returncode:
        details = result.stderr.decode("utf-8", errors="replace").strip()
        raise PilotError(f"cannot read pinned upstream {UPSTREAM} (git exit {result.returncode})\n{details}")
    return result.stdout


def run_fixture(case: str, target: str, review_adopt: bool) -> None:
    from skillopt_sleep.backend import MockBackend
    from skillopt_sleep.config import DEFAULTS, SleepConfig
    from skillopt_sleep.cycle import run_sleep_cycle
    from skillopt_sleep.staging import StagingError, adopt
    from skillopt_sleep.types import TaskRecord

    run_dir = Path(tempfile.mkdtemp(prefix="skillopt-sleep-pilot-")).resolve()
    project = run_dir / "project"
    fixture_home = run_dir / "fixture-home"
    skill_path = project / SKILL_FILE
    memory_path = project / MEMORY_FILE
    skill_path.parent.mkdir(parents=True)
    skill_base = (
        "---\nname: pilot\ndescription: Synthetic pilot skill.\n---\n\n"
        + MockBackend.RULE_TEXT["json-only"] + "\n"
    ).encode("utf-8")
    memory_base = b"# Synthetic pilot memory\n"
    skill_path.write_bytes(skill_base)
    memory_path.write_bytes(memory_base)

    class RegressingMock(MockBackend):
        def attempt(self, task, skill, memory, sample_id=0):
            if task.id == "keep-json" and self.RULE_TEXT["wrap-answer"] in skill + memory:
                return "Regressed formatting."
            return super().attempt(task, skill, memory, sample_id=sample_id)

    cfg = SleepConfig(dict(
        DEFAULTS,
        invoked_project=str(project), state_dir=str(run_dir / "state"),
        claude_home=str(fixture_home / "claude"), codex_home=str(fixture_home / "codex"),
        cursor_home=str(fixture_home / "cursor"), pi_home=str(fixture_home / "pi"),
        target_skill_path=str(skill_path),
        backend="mock", gate_mode="on", gate_metric="hard", gate_no_regression=True,
        evolve_skill=target == "skill", evolve_memory=target == "memory", auto_adopt=False,
    ))
    # Distinct intents and IDs keep training separate from validation.
    task_specs = [
        ("learn-wrap", "train", "wrap-answer", "Render the synthetic room label", "AzureRook"),
        ("val-wrap-a", "val", "wrap-answer", "Render the synthetic catalog ID", "JadeOrbit"),
        ("val-wrap-b", "val", "wrap-answer", "Render the synthetic sample ID", "StoneGull"),
        ("keep-json", "val", "json-only", "Return a synthetic JSON object", '{"ok":true}'),
    ]
    tasks = [
        TaskRecord(id=key, project=str(project), intent=intent, split=split, reference_kind="exact",
                   reference=reference, tags=[f"rule:{rule}"])
        for key, split, rule, intent, reference in task_specs
    ]
    backend = RegressingMock() if case == "regression" else MockBackend()
    outcome = run_sleep_cycle(cfg, seed_tasks=tasks, backend=backend)
    stage = Path(outcome.staging_dir)
    proposal_name = "proposed_SKILL.md" if target == "skill" else "proposed_CLAUDE.md"
    proposal_path = stage / proposal_name
    target_path, baseline = (skill_path, skill_base) if target == "skill" else (memory_path, memory_base)
    expected_accept = case == "pass"
    if (outcome.adopted or outcome.report.holdout_leaked or outcome.report.accepted != expected_accept
            or proposal_path.is_file() != expected_accept
            or skill_path.read_bytes() != skill_base or memory_path.read_bytes() != memory_base):
        raise PilotError(f"unexpected mock cycle result; inspect {stage}")

    diff_path = None
    if outcome.report.accepted:
        rel = target_path.relative_to(project).as_posix()
        proposal = proposal_path.read_bytes()
        manifest_path = stage / "manifest.json"
        manifest = manifest_path.read_bytes()
        diff = "".join(difflib.unified_diff(
            baseline.decode("utf-8").splitlines(keepends=True),
            proposal.decode("utf-8").splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}",
        ))
        diff_path = run_dir / "candidate.diff"
        diff_path.write_text(diff, encoding="utf-8", newline="\n")

    summary = {
        "upstream": UPSTREAM, "case": case, "target": target, "backend": backend.name,
        "accepted": outcome.report.accepted, "gate": outcome.report.gate_action, "adopted": False,
        "run_dir": str(run_dir), "staging_dir": str(stage),
        "candidate_diff": str(diff_path) if diff_path else None,
    }
    print(json.dumps(summary), flush=True)
    if review_adopt and diff_path:
        print(diff, file=sys.stderr, end="")
        print("Type ADOPT to apply this proposal to the disposable fixture: ", file=sys.stderr, end="", flush=True)
        try:
            response = input()
        except EOFError:
            response = ""
        if response == "ADOPT":
            if proposal_path.read_bytes() != proposal or manifest_path.read_bytes() != manifest:
                raise PilotError("the staged proposal or manifest changed during review")
            try:
                changed = adopt(str(stage), project_root=str(project), skill_roots=[], target_skill_path=str(skill_path))
            except StagingError as exc:
                raise PilotError(str(exc)) from exc
            if changed != [str(target_path)]:
                raise PilotError(f"unexpected adoption result; inspect {stage}")
            print(json.dumps({"adopted": True, "paths": changed}), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    modes = parser.add_subparsers(dest="mode", required=True)
    mark = modes.add_parser("mark", allow_abbrev=False, help="record a SessionEnd marker")
    mark.add_argument("--marker-dir", required=True, type=Path)
    mock = modes.add_parser("mock", allow_abbrev=False, help="stage a proposal using synthetic tasks")
    mock.add_argument("--engine-repo", required=True, type=Path)
    mock.add_argument("--case", choices=["pass", "regression"], default="pass")
    mock.add_argument("--target", choices=["skill", "memory"], default="skill")
    mock.add_argument("--review-adopt", action="store_true", help="review in a terminal and adopt into this new fixture")
    args = parser.parse_args()
    try:
        if args.mode == "mark":
            mark_session(args.marker_dir)
        else:
            if args.review_adopt and not sys.stdin.isatty():
                raise PilotError("--review-adopt requires an interactive terminal")
            if not sys.flags.isolated or not sys.flags.no_site or not sys.dont_write_bytecode:
                raise PilotError("run mock with Python -I -B -S -X utf8")
            source = upstream_zip(args.engine_repo)
            with tempfile.TemporaryDirectory(prefix="skillopt-sleep-engine-") as engine_dir:
                with zipfile.ZipFile(io.BytesIO(source)) as archive:
                    archive.extractall(engine_dir)
                sys.path.insert(0, engine_dir)
                run_fixture(args.case, args.target, args.review_adopt)
    except (PilotError, OSError, subprocess.TimeoutExpired, zipfile.BadZipFile) as exc:
        print(f"sleep Windows pilot: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
