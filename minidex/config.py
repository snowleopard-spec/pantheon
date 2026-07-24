from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    sec_user_agent: str

    repo_root: Path
    data_dir: Path
    raw_dir: Path
    outputs_dir: Path
    db_path: Path

    definitions_path: Path
    prompt_path: Path

    embedding_model: str
    similarity_threshold: float
    score_floor: float

    batch_model: str
    prompt_version: str

    max_item1_chars: int = 12_000

    def file_sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


_VERSION_RE = re.compile(r"v(\d+\.\d+)")


def _read_prompt_version(prompt_path: Path) -> str:
    first_line = prompt_path.read_text(encoding="utf-8").splitlines()[0]
    m = _VERSION_RE.search(first_line)
    if not m:
        raise RuntimeError(
            f"Could not parse prompt version from first line of {prompt_path}: {first_line!r}"
        )
    return m.group(1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    sec_ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing from environment / .env")
    if not sec_ua:
        raise RuntimeError("SEC_USER_AGENT missing from environment / .env")

    data_dir = REPO_ROOT / "data"
    outputs_dir = REPO_ROOT / "outputs"
    raw_dir = data_dir / "raw"
    for d in (data_dir, raw_dir, outputs_dir):
        d.mkdir(parents=True, exist_ok=True)

    definitions_path = REPO_ROOT / "definitions" / "minidex_definitions.yaml"
    prompt_path = REPO_ROOT / "prompts" / "scoring_prompt.md"

    return Settings(
        anthropic_api_key=api_key,
        sec_user_agent=sec_ua,
        repo_root=REPO_ROOT,
        data_dir=data_dir,
        raw_dir=raw_dir,
        outputs_dir=outputs_dir,
        db_path=data_dir / "minidex.db",
        definitions_path=definitions_path,
        prompt_path=prompt_path,
        embedding_model=os.environ.get("MINIDEX_EMBED_MODEL", "BAAI/bge-large-en-v1.5"),
        similarity_threshold=float(os.environ.get("MINIDEX_SIM_THRESHOLD", "0.60")),
        score_floor=float(os.environ.get("MINIDEX_SCORE_FLOOR", "0.10")),
        batch_model=os.environ.get("MINIDEX_BATCH_MODEL", "claude-haiku-4-5"),
        prompt_version=_read_prompt_version(prompt_path),
    )
