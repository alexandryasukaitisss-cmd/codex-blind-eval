import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import blind_eval as app

TASKS = [{"id": "one", "prompt": "A synthetic evaluation question."}]
VARIANTS = {"simple": {"instructions": "First approach."}, "verified": {"instructions": "Second approach."}}
CLI = Path(app.__file__).absolute()

FAKE_CODEX = """
import os
from pathlib import Path
import sys

args = sys.argv[1:]
assert args[:2] == ["-a", "never"]
assert args[args.index("--sandbox") + 1] == "read-only"
assert "--ignore-user-config" in args and "--ignore-rules" in args
assert "--ephemeral" in args
assert {args[i+1] for i, arg in enumerate(args) if arg == "--disable"} >= {
    "shell_tool", "unified_exec", "apps", "hooks", "plugins", "browser_use", "computer_use"
}
assert 'cli_auth_credentials_store="file"' in args
assert "OPENAI_API_KEY" not in os.environ
assert sys.stdin.read() == "A synthetic evaluation question."
profile = Path(os.environ["CODEX_HOME"])
assert not (profile / "auth.json").exists()
assert not (profile / "config.toml").exists()
out = Path(args[args.index("-o") + 1])
instructions = Path("AGENTS.md").read_text().strip()
out.write_text("Reply using " + instructions)
if instructions == "Second approach." and not (profile / "simulated-failure").exists():
    (profile / "simulated-failure").touch()
    raise SystemExit(9)
"""

FAKE_CLAUDE = """
from pathlib import Path
import sys

args = sys.argv[1:]
assert "-p" in args
assert args[args.index("--output-format") + 1] == "text"
assert "--no-session-persistence" in args
assert args[args.index("--tools") + 1] == ""
assert "--disable-slash-commands" in args
assert "--no-chrome" in args
assert "--strict-mcp-config" in args
prompt = sys.stdin.read()
assert prompt == "A synthetic evaluation question."
sys_prompt = args[args.index("--append-system-prompt") + 1]
model = args[args.index("--model") + 1] if "--model" in args else "default"
effort = args[args.index("--effort") + 1] if "--effort" in args else "default"
assert Path("CLAUDE.md").read_text().strip() == sys_prompt
print(f"Claude[{model}/{effort}] {sys_prompt}")
"""

FAKE_GENERIC = """
import os
from pathlib import Path
import sys

prompt = sys.stdin.read()
instructions = Path(sys.argv[1]).read_text().strip()
assert Path("AGENTS.md").read_text().strip() == instructions
assert Path("CLAUDE.md").read_text().strip() == instructions
assert os.environ["BLIND_EVAL_INSTRUCTIONS_FILE"] == sys.argv[1]
reply = f"Generic({instructions}): {prompt}"
if len(sys.argv) > 2:
    Path(sys.argv[2]).write_text(reply, encoding="utf-8")
else:
    print(reply)
"""


class BlindEvalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "run"

    def test_offline_cli_ballot_and_reveal(self):
        subprocess.run([sys.executable, "-B", CLI, "demo", self.root], check=True, capture_output=True)
        sheet = json.loads((self.root / "blind.json").read_text())
        self.assertEqual(set(sheet[0]), {"id", "prompt", "A", "B"})
        self.assertNotIn("baseline", json.dumps(sheet))
        self.assertFalse(list((self.root / "profile").iterdir()))
        letter = next(letter for letter in app.LETTERS if "sharing an ID" in sheet[0][letter])
        (self.root / "votes.json").write_text(json.dumps({"validate-csv": letter}))
        revealed = subprocess.run(
            [sys.executable, "-B", CLI, "reveal", self.root], check=True, capture_output=True,
        )
        self.assertEqual(json.loads(revealed.stdout)["wins"], {"baseline": 0, "verify": 1})

    def test_incomplete_attempt_never_becomes_a_vote_and_can_resume(self):
        runner = self.root.parent / "fake-codex"
        runner.write_text(f"#!{sys.executable}\n" + FAKE_CODEX, encoding="utf-8")
        runner.chmod(0o700)
        app.prepare(self.root, TASKS, VARIANTS)
        plan = app.read_run(self.root)
        key = plan["pairs"]["one"]["key"]
        first = next(letter for letter, name in key.items() if name == "simple")
        second = next(letter for letter, name in key.items() if name == "verified")
        plan["pairs"]["one"]["order"] = [first, second]
        app.write_json(self.root / "run.json", plan)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-value-for-boundary-test"}):
            with self.assertRaisesRegex(ValueError, "status 9"):
                app.run(self.root, 5, executable=str(runner))
            successful = self.root / "answers" / f"one-{first}.txt"
            inode = successful.stat().st_ino
            self.assertFalse((self.root / "answers" / f"one-{second}.txt").exists())
            self.assertFalse((self.root / "blind.json").exists())
            app.run(self.root, 5, executable=str(runner))
        self.assertEqual(successful.stat().st_ino, inode)
        self.assertEqual(len(app.load_json(self.root / "blind.json")), 1)

    def test_claude_runner_isolates_tools_and_maps_model_and_effort(self):
        runner = self.root.parent / "fake-claude"
        runner.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE, encoding="utf-8")
        runner.chmod(0o700)
        variants = {
            "simple": {"instructions": "First approach.", "model": "sonnet", "model_reasoning_effort": "minimal"},
            "verified": {"instructions": "Second approach.", "model": "opus", "model_reasoning_effort": "high"},
        }
        app.prepare(self.root, TASKS, variants)
        app.run(self.root, 5, runner="claude", executable=str(runner))
        sheet = app.load_json(self.root / "blind.json")
        answers = {sheet[0]["A"].strip(), sheet[0]["B"].strip()}
        self.assertEqual(
            answers,
            {"Claude[sonnet/low] First approach.", "Claude[opus/high] Second approach."},
        )

    def test_generic_command_runner_supports_stdout_and_output_file(self):
        script = self.root.parent / "fake_generic.py"
        script.write_text(FAKE_GENERIC, encoding="utf-8")

        # 1. stdout mode
        app.prepare(self.root, TASKS, VARIANTS)
        template_stdout = f'"{sys.executable}" "{script}" "{{instructions_file}}"'
        app.run(self.root, 5, runner="command", command_template=template_stdout)
        sheet = app.load_json(self.root / "blind.json")
        self.assertIn("Generic(First approach.)", sheet[0]["A"] + sheet[0]["B"])
        self.assertIn("Generic(Second approach.)", sheet[0]["A"] + sheet[0]["B"])

        # 2. {output_file} mode
        root2 = self.root.parent / "run-file"
        app.prepare(root2, TASKS, VARIANTS)
        template_file = f'"{sys.executable}" "{script}" "{{instructions_file}}" "{{output_file}}"'
        app.run(root2, 5, runner="command", command_template=template_file)
        sheet2 = app.load_json(root2 / "blind.json")
        self.assertIn("Generic(Second approach.)", sheet2[0]["A"] + sheet2[0]["B"])

    def test_command_runner_requires_template(self):
        app.prepare(self.root, TASKS, VARIANTS)
        with self.assertRaisesRegex(ValueError, "command-template"):
            app.run(self.root, 5, runner="command")

    def test_rejects_path_ids_and_arbitrary_codex_configuration(self):
        with self.assertRaises(ValueError):
            app.prepare(self.root, [{"id": "../outside", "prompt": "Task"}], VARIANTS)
        self.assertFalse(self.root.exists())
        variants = {**VARIANTS, "simple": {**VARIANTS["simple"], "mcp_servers": {"local": {}}}}
        with self.assertRaises(ValueError):
            app.prepare(self.root, TASKS, variants)
        self.assertFalse(self.root.exists())

    def test_reveal_rejects_missing_duplicate_and_unknown_votes(self):
        app.demo(self.root)
        for bad in ('{}', '{"validate-csv": "A", "validate-csv": "B"}', '{"validate-csv": "C"}'):
            (self.root / "votes.json").write_text(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.reveal(self.root)
        for vote, counter in (("tie", "ties"), ("skip", "skips")):
            app.write_json(self.root / "votes.json", {"validate-csv": vote})
            result = app.reveal(self.root)
            self.assertEqual(result[counter], 1)
            self.assertEqual(sum(result["wins"].values()), 0)

    def test_rejects_symlinked_artifact(self):
        app.demo(self.root)
        outside = self.root.parent / "outside.txt"
        outside.write_text("Should not be read.")
        slot = self.root / "answers" / "validate-csv-A.txt"
        slot.unlink()
        slot.symlink_to(outside)
        app.write_json(self.root / "votes.json", {"validate-csv": "A"})
        with self.assertRaisesRegex(ValueError, "regular file"):
            app.reveal(self.root)


if __name__ == "__main__":
    unittest.main()
