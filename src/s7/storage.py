"""Content-addressed storage. Every downloaded or derived file lives at
data/downloads/{sha256[:2]}/{sha256} -- re-running a paper never re-downloads
or re-writes a file that's already there.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def path_for(downloads_dir: Path, sha256: str) -> Path:
    return downloads_dir / sha256[:2] / sha256


def store_bytes(downloads_dir: Path, data: bytes) -> tuple[str, Path]:
    """Write `data` if not already present. Returns (sha256, storage_path)."""
    digest = sha256_of(data)
    path = path_for(downloads_dir, digest)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return digest, path
