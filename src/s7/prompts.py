"""Loads prompts/*.md. Prompts are the most frequently edited part of the
system and need to be diffable, so they live as files, not inline strings.

Each file holds a system half and a user half (a Jinja2 template, rendered
with stage-supplied context), separated by a `<!-- USER -->` marker line.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Template

_USER_MARKER = "<!-- USER -->"


def find_prompts_dir() -> Path:
    """Walk up from cwd, then from this file, until prompts/ is found."""
    candidates = [Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents]
    for base in candidates:
        candidate = base / "prompts"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("Could not find prompts/. Run s7 from the repository root.")


def render_prompt(name: str, **context: object) -> tuple[str, str]:
    """Load prompts/{name}.md and render both halves with `context`.

    Returns (system, user). The system half is rendered too, in case it
    ever needs shared constants, but the current templates only interpolate
    into the user half.
    """
    path = find_prompts_dir() / f"{name}.md"
    raw = path.read_text()
    if _USER_MARKER not in raw:
        raise ValueError(f"{path} has no {_USER_MARKER} marker separating system/user")
    system_raw, user_raw = raw.split(_USER_MARKER, 1)
    system = Template(system_raw.strip()).render(**context)
    user = Template(user_raw.strip()).render(**context)
    return system, user
