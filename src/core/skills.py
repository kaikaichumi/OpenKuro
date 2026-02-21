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

Built-in skills are shipped in the ./skills/ directory alongside the source code.
User skills go in ~/.kuro/skills/ or any configured skills_dirs.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Built-in skills directory (relative to project root)
_BUILTIN_SKILLS_DIR: Path | None = None


def _get_builtin_skills_dir() -> Path:
    """Get the built-in skills directory (./skills/ in the project root)."""
    global _BUILTIN_SKILLS_DIR
    if _BUILTIN_SKILLS_DIR is None:
        # Navigate from src/core/skills.py -> src/core -> src -> project root -> skills
        _BUILTIN_SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"
    return _BUILTIN_SKILLS_DIR


@dataclass
class Skill:
    """A parsed skill from a SKILL.md file."""

    name: str
    description: str
    content: str  # Full markdown body (after frontmatter)
    path: Path  # Source file path
    source: str  # "builtin" | "local" | "project"
    tags: list[str] = field(default_factory=list)


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
        """Scan configured skill directories and built-in skills for SKILL.md files."""
        # 1. Discover built-in skills (shipped with Kuro)
        builtin_dir = _get_builtin_skills_dir()
        if builtin_dir.is_dir():
            for child in builtin_dir.iterdir():
                if not child.is_dir():
                    continue
                skill_file = child / "SKILL.md"
                if skill_file.is_file():
                    skill = self._parse_skill_file(skill_file, source="builtin")
                    if skill:
                        self._skills[skill.name] = skill

        # 2. Discover user skills from configured directories
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
                        # User skills override built-in skills with same name
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

    # ------------------------------------------------------------------
    # Install / Uninstall
    # ------------------------------------------------------------------

    def install_skill(self, skill_name: str, target_dir: str | None = None) -> str | None:
        """Install a built-in skill to the user's skills directory.

        Copies from the built-in ./skills/ directory to the user's
        configured skills directory (default: first entry in skills_dirs).

        Returns the installed path string, or None if failed.
        """
        builtin_dir = _get_builtin_skills_dir()
        source_dir = builtin_dir / skill_name

        if not source_dir.is_dir():
            logger.warning("Skill '%s' not found in built-in catalog", skill_name)
            return None

        # Determine target directory
        if target_dir:
            dest_base = Path(target_dir).expanduser()
        elif self._config and self._config.skills_dirs:
            dest_base = Path(self._config.skills_dirs[0]).expanduser()
        else:
            from src.config import get_kuro_home
            dest_base = get_kuro_home() / "skills"

        dest_dir = dest_base / skill_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Copy SKILL.md
            src_file = source_dir / "SKILL.md"
            dest_file = dest_dir / "SKILL.md"
            shutil.copy2(str(src_file), str(dest_file))

            # Parse and register the newly installed skill
            skill = self._parse_skill_file(dest_file, source="local")
            if skill:
                self._skills[skill.name] = skill

            logger.info("Installed skill '%s' to %s", skill_name, dest_dir)
            return str(dest_dir)

        except Exception as e:
            logger.error("Failed to install skill '%s': %s", skill_name, e)
            return None

    def uninstall_skill(self, skill_name: str) -> bool:
        """Remove a user-installed skill (not built-in).

        Returns True if successfully removed.
        """
        skill = self._skills.get(skill_name)
        if not skill:
            return False

        if skill.source == "builtin":
            logger.warning("Cannot uninstall built-in skill '%s'", skill_name)
            return False

        try:
            # Remove the skill directory
            skill_dir = skill.path.parent
            if skill_dir.is_dir():
                shutil.rmtree(str(skill_dir))

            # Deactivate and unregister
            self._active.discard(skill_name)
            del self._skills[skill_name]
            return True

        except Exception as e:
            logger.error("Failed to uninstall skill '%s': %s", skill_name, e)
            return False

    # ------------------------------------------------------------------
    # Search / Catalog
    # ------------------------------------------------------------------

    def list_available_skills(self) -> list[dict[str, str]]:
        """List all skills available for installation (built-in catalog).

        Returns list of dicts with name, description, installed status.
        """
        builtin_dir = _get_builtin_skills_dir()
        available: list[dict[str, str]] = []

        if not builtin_dir.is_dir():
            return available

        for child in sorted(builtin_dir.iterdir()):
            if not child.is_dir():
                continue
            skill_file = child / "SKILL.md"
            if skill_file.is_file():
                skill = self._parse_skill_file(skill_file, source="builtin")
                if skill:
                    installed = skill.name in self._skills and self._skills[skill.name].source == "local"
                    available.append({
                        "name": skill.name,
                        "description": skill.description,
                        "installed": "yes" if installed else "no",
                        "source": "builtin",
                    })

        return available

    def search_skills(self, query: str) -> list[Skill]:
        """Search skills by name or description keyword."""
        query_lower = query.lower()
        results = []
        for skill in self._skills.values():
            if (query_lower in skill.name.lower()
                    or query_lower in skill.description.lower()
                    or query_lower in skill.content.lower()[:500]):
                results.append(skill)
        return results

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def skill_count(self) -> int:
        return len(self._skills)

    @property
    def active_count(self) -> int:
        return len(self._active)
