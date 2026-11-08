"""Pairwise, identity-blind review of two Codex configurations."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

LETTERS = ("A", "B")
CHOICES = (*LETTERS, "tie", "skip")
SLUG = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
MODEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}\Z")
SETTINGS = {"model", "model_reasoning_effort"}
EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
DISABLED_FEATURES = (
    "apps", "hooks", "shell_tool", "unified_exec", "shell_snapshot",
    "plugins", "skill_search", "memories", "multi_agent",
    "browser_use", "browser_use_external", "in_app_browser", "computer_use",
)


def checked_text(value, label, limit=24_000, *, allow_empty=False):
    if (not isinstance(value, str) or len(value) > limit or "\0" in value
            or any(0xD800 <= ord(c) <= 0xDFFF for c in value)
            or (not allow_empty and not value.strip())):
        raise ValueError(f"invalid {label}")
    return value


def require_fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() <= set(required) | set(optional):
        raise ValueError(f"expected fields {', '.join(required)}; optional {', '.join(optional)}")


def validate(tasks, variants):
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 100:
        raise ValueError("provide 1–100 tasks")
    seen = set()
    for task in tasks:
        require_fields(task, ("id", "prompt"))
        name = task["id"]
        if not isinstance(name, str) or not SLUG.fullmatch(name) or name in seen:
            raise ValueError("task IDs must be unique lowercase slugs")
        seen.add(name)
        checked_text(task["prompt"], "task prompt")
    if not isinstance(variants, dict) or len(variants) != 2:
        raise ValueError("provide exactly two variants")
    for name, variant in variants.items():
        if not isinstance(name, str) or not SLUG.fullmatch(name):
            raise ValueError("variant names must be lowercase slugs")
        require_fields(variant, ("instructions",), SETTINGS)
        instructions = checked_text(variant["instructions"], "variant instructions", allow_empty=True)
        if len(instructions.encode("utf-8")) > 24_000:
            raise ValueError("variant instructions exceed 24 KB")
        if "model" in variant and (not isinstance(variant["model"], str) or not MODEL.fullmatch(variant["model"])):
            raise ValueError("invalid model name")
        if "model_reasoning_effort" in variant and (
            not isinstance(variant["model_reasoning_effort"], str)
            or variant["model_reasoning_effort"] not in EFFORTS
        ):
            raise ValueError("invalid model reasoning effort")


def load_toml(path, key):
    if path.stat().st_size > 3_000_000:
        raise ValueError("TOML input exceeds 3 MB")
    with path.open("rb") as source:
        data = tomllib.load(source)
    require_fields(data, (key,))
    return data[key]


def load_json(path):
    def pairs(items):
        result = dict(items)
        if len(result) != len(items):
            raise ValueError("duplicate JSON key")
        return result

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected regular file: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)


def atomic_write(path, text):
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".writing-", encoding="utf-8", mode="w", delete=False
    ) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(text)
            stream.close()
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare(root, tasks, variants, *, demo=False):
    validate(tasks, variants)
    root.mkdir(mode=0o700)
    for name in ("profile", "answers"):
        (root / name).mkdir(mode=0o700)
    rng = random.Random(1) if demo else random.SystemRandom()
    pairs = {}
    for task in tasks:
        assignment = dict(zip(LETTERS, rng.sample(list(variants), k=2)))
        pairs[task["id"]] = {"key": assignment, "order": rng.sample(LETTERS, k=2)}
    write_json(root / "run.json", {
        "format": 1, "demo": demo, "tasks": tasks, "variants": variants, "pairs": pairs,
    })


def read_run(root):
    if root.is_symlink() or any((root / name).is_symlink() or not (root / name).is_dir()
                                for name in ("profile", "answers")):
        raise ValueError("invalid run directory")
    plan = load_json(root / "run.json")
    require_fields(plan, ("format", "demo", "tasks", "variants", "pairs"))
    if type(plan["format"]) is not int or plan["format"] != 1 or type(plan["demo"]) is not bool:
        raise ValueError("unsupported run format")
    validate(plan["tasks"], plan["variants"])
    require_fields(plan["pairs"], [task["id"] for task in plan["tasks"]])
    for pair in plan["pairs"].values():
        require_fields(pair, ("key", "order"))
        require_fields(pair["key"], LETTERS)
        a, b = pair["key"]["A"], pair["key"]["B"]
        if (a, b) not in (tuple(plan["variants"]), tuple(reversed(plan["variants"]))) or pair["order"] not in (
            ["A", "B"], ["B", "A"]
        ):
            raise ValueError("invalid pair assignment")
    return plan


def answer_path(root, task_id, letter):
    path = root / "answers" / f"{task_id}-{letter}.txt"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("answer path is not a regular file")
    return path


def stored_answer(path):
    if not path.exists():
        return None
    if path.stat().st_size > 512_000:
        raise ValueError("saved answer exceeds 512 KB")
    return checked_text(path.read_text(encoding="utf-8"), "saved answer", limit=128_000)


@contextmanager
def lock(root):
    busy = root / ".busy"
    busy.mkdir(mode=0o700)
    try:
        yield
    finally:
        busy.rmdir()


def invoke_codex(executable, profile, task, variant, timeout):
    # The runner evaluates prompt/instruction changes. Keep connectors, shell tools,
    # hooks, user configuration and local execution rules out of both conditions.
    flags = [
        "-a", "never", "-c", 'web_search="disabled"',
        "-c", 'cli_auth_credentials_store="file"',
        "exec", "--sandbox", "read-only", "--ephemeral",
        "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
    ]
    for feature in DISABLED_FEATURES:
        flags.extend(("--disable", feature))
    for key in sorted(SETTINGS & variant.keys()):
        flags.extend(("-c", f"{key}={json.dumps(variant[key])}"))
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE") if key in os.environ}
    env["CODEX_HOME"] = str(profile)
    with tempfile.TemporaryDirectory(prefix="codex-blind-task-") as scratch:
        work = Path(scratch) / "task"
        work.mkdir(mode=0o700)
        (work / "AGENTS.md").write_text(variant["instructions"] + "\n", encoding="utf-8")
        output = Path(scratch) / "reply.txt"
        result = subprocess.run(
            [executable, *flags, "-o", str(output), "-"], input=task["prompt"],
            cwd=work, env=env, encoding="utf-8", stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=timeout, check=False,
        )
        if result.returncode:
            raise ValueError(f"Codex exited with status {result.returncode}")
        if output.is_symlink() or not output.is_file() or output.stat().st_size > 512_000:
            raise ValueError("Codex returned no usable final answer")
        return checked_text(output.read_text(encoding="utf-8"), "Codex answer", limit=128_000)


def blind_sheet(root, plan):
    sheet = []
    for task in plan["tasks"]:
        answers = {letter: stored_answer(answer_path(root, task["id"], letter)) for letter in LETTERS}
        if any(text is None for text in answers.values()):
            raise ValueError("complete all answer pairs before judging")
        sheet.append({"id": task["id"], "prompt": task["prompt"], **answers})
    write_json(root / "blind.json", sheet)
    if not (root / "votes.json").exists():
        write_json(root / "votes.json", {task["id"]: "" for task in plan["tasks"]})


def run(root, timeout, *, executable=None):
    root = root.absolute()
    plan = read_run(root)
    if plan["demo"]:
        raise ValueError("the demo does not use Codex")
    executable = executable or shutil.which("codex")
    if not executable:
        raise ValueError("Codex CLI not found")
    executable = os.path.abspath(executable)
    with lock(root):
        for task in plan["tasks"]:
            pair = plan["pairs"][task["id"]]
            for letter in pair["order"]:
                path = answer_path(root, task["id"], letter)
                if stored_answer(path) is not None:
                    continue
                variant = plan["variants"][pair["key"][letter]]
                try:
                    reply = invoke_codex(executable, root / "profile", task, variant, timeout)
                except subprocess.TimeoutExpired:
                    raise ValueError(f"{task['id']} {letter}: Codex timed out; run again to resume") from None
                except (OSError, ValueError) as exc:
                    # Do not print stderr or paths from the runner.
                    reason = str(exc) if type(exc) is ValueError else "could not start Codex"
                    raise ValueError(f"{task['id']} {letter}: {reason}; run again to resume") from None
                atomic_write(path, reply)
        blind_sheet(root, plan)


def reveal(root):
    plan = read_run(root)
    with lock(root):
        votes = load_json(root / "votes.json")
        require_fields(votes, [task["id"] for task in plan["tasks"]])
        result = {"wins": dict.fromkeys(sorted(plan["variants"]), 0), "ties": 0, "skips": 0, "pairs": []}
        for task_id, vote in votes.items():
            if vote not in CHOICES:
                raise ValueError(f"vote for {task_id} must be A, B, tie or skip")
            key = plan["pairs"][task_id]["key"]
            if any(stored_answer(answer_path(root, task_id, letter)) is None for letter in LETTERS):
                raise ValueError("cannot reveal an incomplete pair")
            if vote in LETTERS:
                result["wins"][key[vote]] += 1
            else:
                result["ties" if vote == "tie" else "skips"] += 1
            result["pairs"].append({"id": task_id, **key, "vote": vote})
    return result


def demo(root):
    tasks = [{"id": "validate-csv", "prompt": "Explain how to verify a CSV import with duplicate rows."}]
    variants = {"baseline": {"instructions": ""}, "verify": {"instructions": "Name a concrete check."}}
    prepare(root, tasks, variants, demo=True)
    plan = read_run(root)
    samples = {
        "baseline": "Inspect the import results and look for duplicate rows.",
        "verify": "Import a fixture with two rows sharing an ID. Verify the duplicate is reported and only one row is stored.",
    }
    key = plan["pairs"]["validate-csv"]["key"]
    for letter in LETTERS:
        atomic_write(answer_path(root, "validate-csv", letter), samples[key[letter]] + "\n")
    blind_sheet(root, plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "prepare", "run", "reveal"):
        sub = commands.add_parser(name)
        sub.add_argument("directory", type=Path)
        if name == "prepare":
            sub.add_argument("--tasks", required=True, type=Path)
            sub.add_argument("--variants", required=True, type=Path)
        if name == "run":
            sub.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    root = args.directory
    try:
        if args.command == "prepare":
            prepare(root, load_toml(args.tasks, "tasks"), load_toml(args.variants, "variants"))
            print("Prepared private run. Sign in to its profile before running Codex.")
        elif args.command == "demo":
            demo(root)
            print("Wrote blind.json and votes.json using fictional answers.")
        elif args.command == "run":
            if not 1 <= args.timeout <= 3600:
                raise ValueError("timeout must be 1–3600 seconds")
            run(root, args.timeout)
            print("Wrote blind.json and votes.json.")
        else:
            print(json.dumps(reveal(root), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
