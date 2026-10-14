"""The Claude Code hooks in .claude/hooks behave as .claude/settings.json promises."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / ".claude" / "hooks"
# Claude Code blocks the tool call when a PreToolUse hook exits 2.
BLOCKED = 2


class HookCommand(BaseModel):
    command: str


class HookGroup(BaseModel):
    matcher: str = ""
    hooks: list[HookCommand]


class Settings(BaseModel):
    hooks: dict[str, list[HookGroup]]


def run_hook(
    name: str, payload: object, project: Path = REPO, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"CLAUDE_PROJECT_DIR": str(project)}
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        ["bash", str(HOOKS / name)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=project,
        timeout=30,
    )


def stub(directory: Path, name: str, script: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / name
    executable.write_text(f"#!/usr/bin/env bash\n{script}\n")
    executable.chmod(0o755)


def test_settings_wire_each_hook_to_its_event_and_matcher() -> None:
    settings = Settings.model_validate_json((REPO / ".claude" / "settings.json").read_text())
    wired = {
        (event, group.matcher, Path(hook.command.split('"')[1]).name)
        for event, groups in settings.hooks.items()
        for group in groups
        for hook in group.hooks
    }
    assert wired == {
        ("SessionStart", "startup", "session-start.sh"),
        ("PreToolUse", "Bash", "no-verify-guard.sh"),
        ("PostToolUse", "Edit|MultiEdit|Write", "format-edited-file.sh"),
        ("Stop", "", "stop-typecheck.sh"),
    }
    assert {name for *_, name in wired} == {path.name for path in HOOKS.glob("*.sh")}


@pytest.mark.parametrize(
    "command",
    [
        "git commit --no-verify -m wip",
        "git push --no-verify",
        "git commit -n -m wip",
        "git commit -anm wip",
        "cd apps && git -C .. commit '--no-verify' -m wip",
        "env SKIP=1 git commit --no-verify",
        "if git commit --no-verify -m wip; then :; fi",
        "(git commit --no-verify -m wip)",
        "{ git push --no-verify; }",
        "! git commit -n -m wip",
    ],
)
def test_no_verify_guard_blocks_hook_bypass(command: str) -> None:
    result = run_hook("no-verify-guard.sh", {"tool_input": {"command": command}})
    assert result.returncode == BLOCKED
    assert "Fix the failing hook" in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'never use --no-verify'",
        "git commit -F - <<'EOF'\nskip --no-verify\nEOF",
        "grep -n -- --no-verify AGENTS.md",
        "git log -n 3",
        "git commit -m wip",
    ],
)
def test_no_verify_guard_allows_commands_that_only_mention_it(command: str) -> None:
    result = run_hook("no-verify-guard.sh", {"tool_input": {"command": command}})
    assert result.returncode == 0, result.stderr


def test_format_hook_formats_the_edited_python_file(tmp_path: Path) -> None:
    edited = tmp_path / "edited.py"
    edited.write_text('import sys\nimport os\nx=  {"a":1}\n')
    result = run_hook("format-edited-file.sh", {"tool_input": {"file_path": str(edited)}})
    assert result.returncode == 0
    assert edited.read_text() == 'import os\nimport sys\n\nx = {"a": 1}\n'


def test_session_start_installs_dependencies_only_when_missing(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    bin_dir = tmp_path / "bin"
    for tool in ("uv", "pnpm"):
        stub(bin_dir, tool, f'echo {tool} "$@" >> {calls}')
    path = f"{bin_dir}:{os.environ['PATH']}"

    first = run_hook("session-start.sh", {}, project=tmp_path, path=path)
    assert "Installed locked" in first.stdout
    assert calls.read_text().splitlines() == [
        "uv sync --locked --quiet",
        "pnpm install --frozen-lockfile --silent",
    ]

    (tmp_path / ".venv").mkdir()
    (tmp_path / "node_modules").mkdir()
    calls.unlink()
    second = run_hook("session-start.sh", {}, project=tmp_path, path=path)
    assert second.stdout == ""
    assert not calls.exists()


def test_stop_hook_reports_type_errors_in_changed_files_once(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "apps" / "observer" / "src" / "broken.py"
    source.parent.mkdir(parents=True)
    source.write_text("x: int = 'one'\n")
    stub(
        tmp_path / ".venv" / "bin",
        "basedpyright",
        'echo "$1:1:10 - error: Type \\"str\\" is not assignable to \\"int\\""; exit 1',
    )

    first = run_hook("stop-typecheck.sh", {"stop_hook_active": False}, project=tmp_path)
    context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
    assert 'apps/observer/src/broken.py:1:10 - error: Type "str"' in context

    continued = run_hook("stop-typecheck.sh", {"stop_hook_active": True}, project=tmp_path)
    assert continued.stdout == ""
