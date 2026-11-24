# Codex Blind Eval

[![CI](https://github.com/alexandryasukaitisss-cmd/codex-blind-eval/actions/workflows/ci.yml/badge.svg)](https://github.com/alexandryasukaitisss-cmd/codex-blind-eval/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/alexandryasukaitisss-cmd/codex-blind-eval)](https://github.com/alexandryasukaitisss-cmd/codex-blind-eval/releases)

[Русский](README.ru.md) · [MIT](LICENSE)

> **Works with Codex CLI, Claude Code (`--runner claude`), and any custom agent/LLM CLI (`--runner command`).**

Did a change to your `AGENTS.md`, `CLAUDE.md`, or system instructions make answers more useful? Give both configurations the same tasks, compare their answers blind as **A** and **B**, then reveal which configuration wrote each one.

The tool randomizes labels and execution order independently for each task. It saves only complete answers, resumes missing attempts, and keeps ties and skipped pairs separate. You supply the tasks and judge the answers.

![Blind A/B terminal reveal output](docs/demo-terminal.png)

## Try it offline

Python 3.11 or newer; no dependencies, account, or network needed for the demo.

~~~sh
mkdir -p runs
python3 blind_eval.py demo runs/demo
~~~

Read **runs/demo/blind.json** in a text editor. Enter **A**, **B**, **tie**, or **skip** for each task in **runs/demo/votes.json**. Reveal the labels:

~~~sh
python3 blind_eval.py reveal runs/demo
~~~

The demo uses fictional answers. The run directory must be new.

## Compare your own instructions

Edit [tasks](examples/tasks.toml) and exactly two [variants](examples/variants.toml). Each variant provides inline instructions (written to `AGENTS.md` / `CLAUDE.md`) and may optionally specify `model` and `model_reasoning_effort`.

### 1. Codex CLI (default)

~~~sh
python3 blind_eval.py prepare runs/trial --tasks examples/tasks.toml --variants examples/variants.toml
CODEX_HOME="$PWD/runs/trial/profile" codex login -c 'cli_auth_credentials_store="file"'
python3 blind_eval.py run runs/trial --runner codex
# Read blind.json and fill in votes.json inside runs/trial.
python3 blind_eval.py reveal runs/trial
~~~

### 2. Claude Code CLI (`--runner claude`)

Runs `claude -p --output-format text --no-session-persistence --tools "" --disable-slash-commands --no-chrome --strict-mcp-config` with `--append-system-prompt` (and optional `--model` / `--effort`) in an isolated per-task directory:

~~~sh
python3 blind_eval.py prepare runs/claude-trial --tasks examples/tasks.toml --variants examples/variants.toml
python3 blind_eval.py run runs/claude-trial --runner claude
python3 blind_eval.py reveal runs/claude-trial
~~~

### 3. Any custom CLI / local LLM (`--runner command`)

Pass any non-shell command template (parsed with `shlex.split`, `shell=False`). The task prompt is always sent on `stdin`. Supported placeholders: `{instructions_file}`, `{output_file}`, `{workdir}`, `{model}`, `{effort}` (also exposed as `BLIND_EVAL_*` environment variables). If `{output_file}` is used, the answer is read from that file; otherwise from `stdout`.

~~~sh
python3 blind_eval.py prepare runs/custom-trial --tasks examples/tasks.toml --variants examples/variants.toml
python3 blind_eval.py run runs/custom-trial --runner command \
  --command-template 'my-agent-cli --system-file "{instructions_file}" --model "{model}"'
python3 blind_eval.py reveal runs/custom-trial
~~~

## Isolation & limits

Each attempt runs in a fresh temporary task directory. For Codex, credentials are isolated in `runs/<name>/profile` with shell tools, hooks, connectors, and web search disabled; for Claude Code, built-in tools, slash commands, Chrome, and session persistence are disabled. Keep the run directory private: it holds your inputs, label key, and answers.

This is a small human evaluation, not a statistical ranking. Answers may reveal their style; one pair per task cannot isolate model variation. The tests use fake CLIs to exercise Codex, Claude Code, custom commands, failure, and resume without any account or network. [Attribution](NOTICE.md).

~~~sh
python3 -B -m unittest discover -s tests -v
~~~
