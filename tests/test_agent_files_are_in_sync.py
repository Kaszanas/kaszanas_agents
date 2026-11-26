from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def load_build_script():
    specification = importlib.util.spec_from_file_location(
        "build_agent_files", REPOSITORY_ROOT / "scripts" / "build_agent_files.py"
    )
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module  # dataclasses resolve annotations via sys.modules
    specification.loader.exec_module(module)
    return module


def test_generated_agent_files_are_up_to_date():
    build_agent_files = load_build_script()
    outputs = build_agent_files.expected_outputs(skills=build_agent_files.load_skills())
    stale = build_agent_files.stale_outputs(outputs=outputs)
    assert not stale, f"stale generated files, run `uv run scripts/build_agent_files.py`: {stale}"


def test_install_into_is_idempotent_and_preserves_existing_agents_md(tmp_path):
    build_agent_files = load_build_script()
    (tmp_path / "AGENTS.md").write_text("# My project\n\nExisting notes.\n", encoding="utf-8")
    skills = build_agent_files.load_skills()
    for _ in range(2):
        build_agent_files.install_into(
            target_project=tmp_path, agents=build_agent_files.ALL_AGENT_TARGETS, skills=skills
        )
    agents_markdown = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert agents_markdown.startswith("# My project\n\nExisting notes.\n")
    assert agents_markdown.count("kaszanas:python-keyword-arguments:start") == 1
    assert "## Python: keyword arguments" in agents_markdown
    assert "\n# Wrong\n" in agents_markdown  # comments inside code fences are not headings
    assert (tmp_path / ".cursor" / "rules" / "python-keyword-arguments.mdc").is_file()
    assert (tmp_path / ".github" / "instructions" / "python-keyword-arguments.instructions.md").is_file()


def test_plugin_manifests_are_valid_json_and_reference_existing_paths():
    marketplace = json.loads((REPOSITORY_ROOT / ".claude-plugin" / "marketplace.json").read_text("utf-8"))
    for plugin in marketplace["plugins"]:
        plugin_root = (REPOSITORY_ROOT / plugin["source"]).resolve()
        manifest = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text("utf-8"))
        assert manifest["name"] == plugin["name"]
        hooks = json.loads((plugin_root / "hooks" / "hooks.json").read_text("utf-8"))
        assert "PostToolUse" in hooks["hooks"]
        assert (plugin_root / "skills" / "python-keyword-arguments" / "SKILL.md").is_file()


def test_repository_dogfoods_its_own_hooks():
    configuration = (REPOSITORY_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    for hook_id in ("kaszanas-lint", "agent-files-sync", "pytest", "ruff-check", "commitizen"):
        assert f"id: {hook_id}" in configuration, f"hook `{hook_id}` missing from .pre-commit-config.yaml"


def test_hook_script_reports_findings_with_exit_code_two(make_project):
    project_root = make_project(files={"main.py": "def area(width, height):\n    return 1\n\narea(1, 2)\n"})
    hook_script = REPOSITORY_ROOT / "plugins" / "kaszanas-python" / "hooks" / "check_edited_python_file.py"
    payload = {"tool_input": {"file_path": str(project_root / "main.py")}, "cwd": str(project_root)}
    scripts_directory = Path(sys.executable).parent
    environment = {"PATH": str(scripts_directory), "SYSTEMROOT": "C:\\Windows"}
    completed = subprocess.run(
        [sys.executable, str(hook_script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 2, completed.stderr
    assert "KWA001" in completed.stderr
    assert "kaszanas-lint check --fix" in completed.stderr

    not_python = {"tool_input": {"file_path": str(project_root / "pyproject.toml")}}
    completed = subprocess.run(
        [sys.executable, str(hook_script)],
        input=json.dumps(not_python),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0
