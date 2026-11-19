"""Targeted tests for the Windows-friendly offline Sleep pilot."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PILOT = REPO / "scripts/sleep_windows_pilot.py"
PYTHON = [sys.executable, "-I", "-B", "-S", "-X", "utf8"]
ENGINE_REPO = Path(os.environ.get("SKILLOPT_PILOT_TEST_ENGINE_REPO", REPO)).resolve()
AUDITED_ENTRY = """
import builtins, os, runpy, sys
from pathlib import Path

def check(event, args):
    if event.startswith('socket.'):
        raise AssertionError(f'network operation in pilot: {event}')
    if event == 'subprocess.Popen':
        command = args[1]
        if isinstance(command, str):
            git_archive = command.startswith('git --no-replace-objects -C ') and ' archive --format=zip ' in command
        else:
            git_archive = command[:3] == ['git', '--no-replace-objects', '-C'] and command[4:6] == ['archive', '--format=zip']
        if not git_archive or args[3].get('GIT_ALLOW_PROTOCOL') != '':
            raise AssertionError(f'unexpected child in pilot: {command}')
    if event == 'open' and isinstance(args[0], str):
        if Path(args[0]) == Path.cwd() / 'private transcript.jsonl':
            raise AssertionError('pilot opened the transcript from the hook payload')

sys.addaudithook(check)
answer = os.environ.get('SKILLOPT_PILOT_TEST_ANSWER')
if answer is not None:
    sys.stdin.isatty = lambda: True
    def reply():
        change = os.environ.get('SKILLOPT_PILOT_TEST_CHANGE')
        if change:
            root, = Path(os.environ['TEMP']).glob('skillopt-sleep-pilot-*')
            project = root / 'project'
            stage, = (project / '.skillopt-sleep' / 'staging').glob('20*')
            if change == 'proposal':
                (stage / 'proposed_CLAUDE.md').write_bytes(b'changed proposal')
            elif change == 'baseline':
                (project / 'CLAUDE.md').write_bytes(b'changed baseline')
        return answer
    builtins.input = reply

sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""


class WindowsPilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skillopt-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "审阅 路径"
        self.root.mkdir()
        (self.root / "private transcript.jsonl").write_bytes(b"synthetic transcript sentinel")
        (self.root / "CLAUDE.md").write_bytes(b"real-project memory sentinel")
        (self.root / "SKILL.md").write_bytes(b"real-project skill sentinel")

    def invoke(self, *args, raw=b"", answer=None, change=None):
        run_temp = tempfile.mkdtemp(dir=self.root, prefix="process-")
        env = dict(os.environ, TEMP=run_temp, TMP=run_temp, TMPDIR=run_temp)
        if answer is not None:
            env["SKILLOPT_PILOT_TEST_ANSWER"] = answer
        if change is not None:
            env["SKILLOPT_PILOT_TEST_CHANGE"] = change
        return subprocess.run(
            [*PYTHON, "-c", AUDITED_ENTRY, str(PILOT), *map(str, args)],
            cwd=self.root, env=env, input=raw, capture_output=True, timeout=30,
        )

    def mock(self, *args, **options):
        return self.invoke("mock", "--engine-repo", ENGINE_REPO, *args, **options)

    def summary(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return json.loads(result.stdout.splitlines()[0])

    def test_session_end_markers_keep_only_digest_and_time_under_concurrent_delivery(self):
        marker_dir = self.root / "会话 标记"
        raw = json.dumps({
            "hook_event_name": "SessionEnd", "session_id": "私有会话 ID",
            "cwd": str(self.root), "transcript_path": str(self.root / "private transcript.jsonl"),
            "reason": "clear",
        }, ensure_ascii=False).encode("utf-8-sig")
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.invoke("mark", "--marker-dir", marker_dir, raw=raw), range(3)))
        for result in results:
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        markers = list(marker_dir.iterdir())
        self.assertEqual(len(markers), 3)
        for path in markers:
            self.assertEqual(path.suffix, ".json")
            record = json.loads(path.read_bytes())
            self.assertEqual(set(record), {"event", "utc", "session_sha256"})
            self.assertEqual(record["event"], "SessionEnd")
            self.assertEqual(record["session_sha256"], hashlib.sha256("私有会话 ID".encode("utf-8")).hexdigest())
            self.assertEqual(datetime.fromisoformat(record["utc"]).utcoffset(), timedelta(0))

    def test_bad_session_end_inputs_fail_before_creating_a_marker(self):
        marker_dir = self.root / "markers"
        for raw in (b"{bad", b"[]", b'{"hook_event_name":"Stop"}', b'{"hook_event_name":"SessionEnd"}', b" " * 65537):
            with self.subTest(input=raw[:50]):
                result = self.invoke("mark", "--marker-dir", marker_dir, raw=raw)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"sleep Windows pilot:", result.stderr)
                self.assertFalse(marker_dir.exists())

    def test_marker_write_failure_is_visible(self):
        marker_dir = self.root / "file instead of directory"
        marker_dir.write_bytes(b"sentinel")
        result = self.invoke("mark", "--marker-dir", marker_dir,
                             raw=b'{"hook_event_name":"SessionEnd","session_id":"synthetic"}')
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"sleep Windows pilot:", result.stderr)
        self.assertEqual(marker_dir.read_bytes(), b"sentinel")

    def test_inert_example_calls_only_the_marker_with_literal_arguments(self):
        config = json.loads((REPO / "docs/sleep-windows-session-end.example.json").read_bytes())
        self.assertEqual(list(config["hooks"]), ["SessionEnd"])
        handler, = config["hooks"]["SessionEnd"][0]["hooks"]
        self.assertEqual(handler["type"], "command")
        self.assertTrue(handler["command"].endswith("/python.exe"))
        self.assertEqual(handler["args"][6:8], ["mark", "--marker-dir"])
        self.assertNotIn("async", handler)

    def test_offline_mock_stages_skill_or_memory_without_adopting(self):
        for target, name in (("skill", "SKILL.md"), ("memory", "CLAUDE.md")):
            with self.subTest(target=target):
                summary = self.summary(self.mock("--target", target))
                stage = Path(summary["staging_dir"])
                root = Path(summary["run_dir"])
                self.assertFalse(Path(summary["run_dir"]).is_relative_to(REPO))
                self.assertTrue(summary["accepted"])
                self.assertFalse(summary["adopted"])
                self.assertEqual(summary["backend"], "mock")
                self.assertEqual([path.name for path in stage.glob("proposed_*.md")], [f"proposed_{name}"])
                diff = Path(summary["candidate_diff"]).read_text(encoding="utf-8")
                self.assertIn(f"/{name}\n", diff)
                self.assertIn("<answer>", diff)
                report = json.loads((stage / "report.json").read_bytes())
                self.assertTrue(report["gate_no_regression"])
                self.assertEqual(report["n_sessions"], 0)
                self.assertAlmostEqual(report["baseline_score"], 1 / 3)
                self.assertEqual(report["candidate_score"], 1.0)
                self.assertFalse((stage / "adopted_legacy.json").exists())
                self.assertEqual((root / "project/CLAUDE.md").read_bytes(), b"# Synthetic pilot memory\n")
                self.assertNotIn(b"<answer>", (root / "project/.claude/skills/pilot/SKILL.md").read_bytes())
        self.assertEqual((self.root / "CLAUDE.md").read_bytes(), b"real-project memory sentinel")
        self.assertEqual((self.root / "SKILL.md").read_bytes(), b"real-project skill sentinel")

    def test_per_task_regression_blocks_an_aggregate_improvement(self):
        result = self.mock("--case", "regression", "--review-adopt", answer="ADOPT")
        summary = self.summary(result)
        stage = Path(summary["staging_dir"])
        report = json.loads((stage / "report.json").read_bytes())
        trial = report["gate_trials"][0]
        self.assertAlmostEqual(trial["baseline_score"], 1 / 3)
        self.assertAlmostEqual(trial["candidate_score"], 2 / 3)
        self.assertTrue(trial["blocked_by_regression"])
        regressed, = [task for task in trial["task_deltas"] if task["status"] == "regressed"]
        self.assertEqual((regressed["task_id"], regressed["baseline_score"], regressed["candidate_score"]),
                         ("keep-json", 1, 0))
        self.assertFalse(summary["accepted"])
        self.assertIsNone(summary["candidate_diff"])
        self.assertEqual(list(stage.glob("proposed_*.md")), [])
        self.assertEqual(result.stderr, b"")

    def test_explicit_adoption_writes_only_fixture_and_records_backup(self):
        for target, relative in (("skill", ".claude/skills/pilot/SKILL.md"), ("memory", "CLAUDE.md")):
            with self.subTest(target=target):
                result = self.mock("--target", target, "--review-adopt", answer="ADOPT")
                summary = self.summary(result)
                stage = Path(summary["staging_dir"])
                fixture = Path(summary["run_dir"]) / "project" / relative
                adopted = json.loads(result.stdout.splitlines()[1])
                self.assertEqual(adopted, {"adopted": True, "paths": [str(fixture)]})
                self.assertEqual(fixture.read_bytes(), (stage / f"proposed_{fixture.name}").read_bytes())
                self.assertNotIn(b"<answer>", (stage / "backup" / fixture.name).read_bytes())
                self.assertTrue((stage / "adopted_legacy.json").is_file())
                self.assertIn(b"Type ADOPT", result.stderr)
        self.assertEqual((self.root / "CLAUDE.md").read_bytes(), b"real-project memory sentinel")
        self.assertEqual((self.root / "SKILL.md").read_bytes(), b"real-project skill sentinel")

    def test_review_refuses_piped_approval_and_accepts_declining(self):
        piped = self.mock("--review-adopt", raw=b"ADOPT\n")
        self.assertEqual(piped.returncode, 1)
        self.assertIn(b"interactive terminal", piped.stderr)
        self.assertFalse(list(self.root.rglob("skillopt-sleep-pilot-*")))
        declined = self.mock("--review-adopt", answer="no")
        summary = self.summary(declined)
        self.assertEqual(len(declined.stdout.splitlines()), 1)
        self.assertFalse((Path(summary["staging_dir"]) / "adopted_legacy.json").exists())

    def test_changed_proposal_or_baseline_stops_adoption(self):
        for change, expected in (("proposal", b"changed during review"), ("baseline", b"changed since staging")):
            with self.subTest(change=change):
                result = self.mock("--target", "memory", "--review-adopt", answer="ADOPT", change=change)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(expected, result.stderr)
                summary = json.loads(result.stdout.splitlines()[0])
                self.assertFalse((Path(summary["staging_dir"]) / "adopted_legacy.json").exists())
                fixture = Path(summary["run_dir"]) / "project/CLAUDE.md"
                baseline = b"changed baseline" if change == "baseline" else b"# Synthetic pilot memory\n"
                self.assertEqual(fixture.read_bytes(), baseline)

    def test_missing_git_pin_fails_without_starting_a_fixture(self):
        empty_repo = self.root / "empty repo"
        subprocess.run(["git", "init", "-q", str(empty_repo)], check=True, capture_output=True, timeout=20)
        result = self.invoke("mock", "--engine-repo", empty_repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"cannot read pinned upstream", result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertFalse(list(self.root.rglob("skillopt-sleep-pilot-*")))


if __name__ == "__main__":
    unittest.main()
