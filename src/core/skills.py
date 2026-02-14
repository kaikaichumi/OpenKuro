"""Skills manager: discovers, loads, and manages SKILL.md files.

Skills are markdown instruction files (compatible with Anthropic/SkillsMP format)
that get injected as SYSTEM messages into the LLM context when activated.

SKILL.md format:
    ---
    name: my-skill
    description: What this skill does
    ---
    # Instructions
    Markdown content here...

Directory structure:
    ~/.kuro/skills/
        my-skill/
            SKILL.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """A parsed skill from a SKILL.md file."""

    name: str
    description: str
    content: str  # Full markdown body (after frontmatter)
    path: Path  # Source file path
    source: str  # "local" | "project"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML frontmatter without PyYAML.

    Supports simple ``key: value`` pairs (sufficient for SKILL.md).
    Returns ``(metadata_dict, body_content)``.
    """
    if not text.startswith("---"):
        return {}, text

    # Split on the second '---' marker
    rest = text[3:]
    end_idx = rest.find("\n---")
    if end_idx == -1:
        return {}, text

    front = rest[:end_idx]
    body = rest[end_idx + 4:]  # skip '\n---'

    metadata: dict[str, str] = {}
    for line in front.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip()

    return metadata, body.strip()


class SkillsManager:
    """Manages discovery, loading, and activation of SKILL.md files."""

    def __init__(self, config: "SkillsConfig | None" = None) -> None:  # noqa: F821
        self._skills: dict[str, Skill] = {}  # name -> Skill
        self._active: set[str] = set()  # Currently active skill names
        self._config = config

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_skills(self) -> None:
        """Scan configured skill directories for SKILL.md files."""
        if self._config is None:
            return

        for dir_str in self._config.skills_dirs:
            skills_dir = Path(dir_str).expanduser()
            if not skills_dir.is_dir():
                continue

            for child in skills_dir.iterdir():
                if not child.is_dir():
                    continue
                skill_file = child / "SKILL.md"
                if skill_file.is_file():
                    skill = self._parse_skill_file(skill_file, source="local")
                    if skill:
                        self._skills[skill.name] = skill

    def _parse_skill_file(self, path: Path, source: str) -> Skill | None:
        """Parse a single SKILL.md file into a Skill object."""
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to read skill file %s: %s", path, e)
            return None

        if not text.strip():
            return None

        metadata, body = _parse_frontmatter(text)

        name = metadata.get("name", path.parent.name)
        description = metadata.get("description", "")

        if not name:
            return None

        return Skill(
            name=name,
            description=description,
            content=body or text,  # If no frontmatter, use entire file
            path=path,
            source=source,
        )

    # ------------------------------------------------------------------
    # Activation
    # ------------------------------------------------------------------

    def activate(self, name: str) -> bool:
        """Activate a skill (will be injected into next LLM context)."""
        if name not in self._skills:
            return False
        self._active.add(name)
        return True

    def deactivate(self, name: str) -> bool:
        """Deactivate a skill."""
        if name not in self._active:
            return False
        self._active.discard(name)
        return True

    def get_active_skills(self) -> list[Skill]:
        """Return currently active skills for context injection."""
        return [self._skills[n] for n in self._active if n in self._skills]

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_skills(self) -> list[Skill]:
        """List all discovered skills."""
        return list(self._skills.values())

    def get_skill(self, name: str) -> Skill | None:
        """Get a skill by name."""
        return self._skills.get(name)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def skill_count(self) -> int:
        return len(self._skills)

    @property
    def active_count(self) -> int:
        return len(self._active)
