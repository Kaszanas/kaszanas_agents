# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Generate every agent-specific file from the skills in `skills/` (the single source of truth).

    uv run scripts/build_agent_files.py                 # regenerate rules/ and plugin copies
    uv run scripts/build_agent_files.py --check         # exit 1 if anything is stale (CI)
    uv run scripts/build_agent_files.py --install-into ../my-project [--agents cursor,copilot,agents-md]

Outputs per skill:
  rules/AGENTS.md                                        section to paste into AGENTS.md
  rules/.cursor/rules/<skill>.mdc                        Cursor project rule (globs from the skill)
  rules/.github/instructions/<skill>.instructions.md     GitHub Copilot path-specific instructions
  plugins/kaszanas-python/skills/<skill>/                copy bundled with the Claude Code plugin
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIRECTORY = REPOSITORY_ROOT / "skills"
RULES_DIRECTORY = REPOSITORY_ROOT / "rules"
PLUGIN_SKILLS_DIRECTORY = REPOSITORY_ROOT / "plugins" / "kaszanas-python" / "skills"
GENERATED_NOTICE = "<!-- Generated from skills/{name}/SKILL.md by scripts/build_agent_files.py -->"
AGENTS_MARKDOWN_START = "<!-- kaszanas:{name}:start -->"
AGENTS_MARKDOWN_END = "<!-- kaszanas:{name}:end -->"
FILE_GLOBS_BY_SKILL_PREFIX = {"python-": "**/*.py"}
ALL_AGENT_TARGETS = ("cursor", "copilot", "agents-md")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    directory: Path

    @property
    def file_glob(self) -> str:
        for prefix, glob in FILE_GLOBS_BY_SKILL_PREFIX.items():
            if self.name.startswith(prefix):
                return glob
        return "**/*"

    @property
    def portable_body(self) -> str:
        """Body without paragraphs linking to files that only exist next to the skill.

        Whole paragraphs are dropped (not single lines) because mdformat may wrap a sentence
        so that the link lands on a line of its own.
        """
        paragraphs = re.split(pattern=r"\n\s*\n", string=self.body.strip())
        kept = [paragraph for paragraph in paragraphs if "references/" not in paragraph]
        return "\n\n".join(kept) + "\n"


def parse_skill(skill_file: Path) -> Skill:
    text = skill_file.read_text(encoding="utf-8")
    match = re.match(r"^---\n(?P<front>.*?)\n---\n(?P<body>.*)$", text, flags=re.DOTALL)
    if match is None:
        raise ValueError(f"{skill_file} has no YAML frontmatter")
    front_matter = dict(
        line.split(":", maxsplit=1) for line in match.group("front").splitlines() if ":" in line
    )
    return Skill(
        name=front_matter["name"].strip(),
        description=front_matter["description"].strip(),
        body=match.group("body").strip() + "\n",
        directory=skill_file.parent,
    )


def load_skills() -> list[Skill]:
    return [parse_skill(skill_file=path) for path in sorted(SKILLS_DIRECTORY.glob("*/SKILL.md"))]


def cursor_rule(skill: Skill) -> str:
    return (
        f"---\ndescription: {skill.description}\nglobs: {skill.file_glob}\nalwaysApply: false\n---\n"
        f"{GENERATED_NOTICE.format(name=skill.name)}\n\n{skill.portable_body}"
    )


def copilot_instructions(skill: Skill) -> str:
    return (
        f'---\napplyTo: "{skill.file_glob}"\n---\n'
        f"{GENERATED_NOTICE.format(name=skill.name)}\n\n{skill.portable_body}"
    )


def demote_markdown_headings(markdown: str) -> str:
    """Add one `#` to every heading so the skill nests under AGENTS.md; code fences untouched."""
    demoted_lines = []
    inside_code_fence = False
    for line in markdown.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            inside_code_fence = not inside_code_fence
        elif not inside_code_fence and re.match(pattern=r"#+ ", string=line):
            line = "#" + line
        demoted_lines.append(line)
    return "".join(demoted_lines)


def agents_markdown_section(skill: Skill) -> str:
    demoted_body = demote_markdown_headings(markdown=skill.portable_body)
    # Blank lines around the HTML comments match mdformat's canonical output, so formatting the
    # host AGENTS.md never fights with re-installing the section.
    return (
        f"{AGENTS_MARKDOWN_START.format(name=skill.name)}\n\n"
        f"{GENERATED_NOTICE.format(name=skill.name)}\n\n{demoted_body}\n"
        f"{AGENTS_MARKDOWN_END.format(name=skill.name)}\n"
    )


def expected_outputs(skills: list[Skill]) -> dict[Path, str]:
    outputs: dict[Path, str] = {}
    outputs[RULES_DIRECTORY / "AGENTS.md"] = "\n".join(
        agents_markdown_section(skill=skill) for skill in skills
    )
    for skill in skills:
        outputs[RULES_DIRECTORY / ".cursor" / "rules" / f"{skill.name}.mdc"] = cursor_rule(skill=skill)
        outputs[RULES_DIRECTORY / ".github" / "instructions" / f"{skill.name}.instructions.md"] = (
            copilot_instructions(skill=skill)
        )
        for source_file in sorted(skill.directory.rglob("*")):
            if source_file.is_file():
                target = PLUGIN_SKILLS_DIRECTORY / skill.name / source_file.relative_to(skill.directory)
                outputs[target] = source_file.read_text(encoding="utf-8")
    return outputs


def stale_outputs(outputs: dict[Path, str]) -> list[Path]:
    stale = []
    for path, content in outputs.items():
        current = path.read_bytes().decode("utf-8") if path.is_file() else None
        if current != content:
            stale.append(path)
    if PLUGIN_SKILLS_DIRECTORY.is_dir():
        stale.extend(
            path for path in PLUGIN_SKILLS_DIRECTORY.rglob("*") if path.is_file() and path not in outputs
        )
    return stale


def write_outputs(outputs: dict[Path, str]) -> None:
    if PLUGIN_SKILLS_DIRECTORY.is_dir():
        shutil.rmtree(PLUGIN_SKILLS_DIRECTORY)
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))


def replace_marked_section(existing: str, section: str, name: str) -> str:
    start = AGENTS_MARKDOWN_START.format(name=name)
    end = AGENTS_MARKDOWN_END.format(name=name)
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"\n?", flags=re.DOTALL)
    if pattern.search(existing):
        return pattern.sub(lambda _: section, existing)
    if not existing or existing.endswith("\n\n"):
        separator = ""
    elif existing.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return existing + separator + section


def install_into(target_project: Path, agents: tuple[str, ...], skills: list[Skill]) -> list[Path]:
    written: list[Path] = []
    for skill in skills:
        if "cursor" in agents:
            path = target_project / ".cursor" / "rules" / f"{skill.name}.mdc"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(cursor_rule(skill=skill).encode("utf-8"))
            written.append(path)
        if "copilot" in agents:
            path = target_project / ".github" / "instructions" / f"{skill.name}.instructions.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(copilot_instructions(skill=skill).encode("utf-8"))
            written.append(path)
        if "agents-md" in agents:
            path = target_project / "AGENTS.md"
            existing = path.read_bytes().decode("utf-8") if path.is_file() else ""
            updated = replace_marked_section(
                existing=existing, section=agents_markdown_section(skill=skill), name=skill.name
            )
            path.write_bytes(updated.encode("utf-8"))
            written.append(path)
    return written


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="fail if generated files are stale")
    parser.add_argument("--install-into", type=Path, help="write rules into another project")
    parser.add_argument("--agents", default=",".join(ALL_AGENT_TARGETS), help="for --install-into")
    parsed = parser.parse_args(arguments)

    skills = load_skills()
    if parsed.install_into is not None:
        agents = tuple(item.strip() for item in parsed.agents.split(",") if item.strip())
        unknown = set(agents) - set(ALL_AGENT_TARGETS)
        if unknown:
            parser.error(f"unknown agent target(s): {', '.join(sorted(unknown))}")
        for path in install_into(target_project=parsed.install_into.resolve(), agents=agents, skills=skills):
            print(f"wrote {path}")
        return 0

    outputs = expected_outputs(skills=skills)
    stale = stale_outputs(outputs=outputs)
    if parsed.check:
        for path in stale:
            print(f"stale: {path.relative_to(REPOSITORY_ROOT).as_posix()}", file=sys.stderr)
        if stale:
            print("run: uv run scripts/build_agent_files.py", file=sys.stderr)
        return 1 if stale else 0
    write_outputs(outputs=outputs)
    print(f"generated {len(outputs)} files from {len(skills)} skill(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
