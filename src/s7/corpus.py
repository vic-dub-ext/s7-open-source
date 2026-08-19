"""Loads corpus/papers.yaml -- the fixed five-paper evaluation set."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def find_corpus_dir() -> Path:
    """Walk up from cwd, then from this file, until corpus/papers.yaml is found."""
    candidates = [Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents]
    for base in candidates:
        candidate = base / "corpus" / "papers.yaml"
        if candidate.exists():
            return base / "corpus"
    raise FileNotFoundError(
        "Could not find corpus/papers.yaml. Run s7 from the repository root."
    )


def load_papers() -> dict[str, dict[str, Any]]:
    corpus_dir = find_corpus_dir()
    data = yaml.safe_load((corpus_dir / "papers.yaml").read_text())
    return {p["key"]: p for p in data["papers"]}
