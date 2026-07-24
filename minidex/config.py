from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_JSON_PATH = REPO_ROOT / "config.json"


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
    anchor_min_weight: float = 0.05

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


def _load_json_config(path: Path) -> dict[str, Any]:
    """Load tuning knobs from config.json if present. Missing file returns {}."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse {path}: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object at the top level")
    return data


def _resolve(
    env_key: str,
    json_key: str,
    default: Any,
    cast: Any,
    json_config: dict[str, Any],
) -> Any:
    """Precedence: env var > config.json > hardcoded default."""
    env_val = os.environ.get(env_key)
    if env_val is not None and env_val != "":
        return cast(env_val)
    if json_key in json_config:
        return cast(json_config[json_key])
    return cast(default)


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

    json_config = _load_json_config(CONFIG_JSON_PATH)

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
        embedding_model=_resolve(
            "MINIDEX_EMBED_MODEL",
            "embedding_model",
            "BAAI/bge-large-en-v1.5",
            str,
            json_config,
        ),
        similarity_threshold=_resolve(
            "MINIDEX_SIM_THRESHOLD",
            "similarity_threshold",
            0.60,
            float,
            json_config,
        ),
        score_floor=_resolve(
            "MINIDEX_SCORE_FLOOR",
            "score_floor",
            0.10,
            float,
            json_config,
        ),
        batch_model=_resolve(
            "MINIDEX_BATCH_MODEL",
            "batch_model",
            "claude-haiku-4-5",
            str,
            json_config,
        ),
        prompt_version=_read_prompt_version(prompt_path),
        max_item1_chars=_resolve(
            "MINIDEX_MAX_ITEM1_CHARS",
            "max_item1_chars",
            12_000,
            int,
            json_config,
        ),
        anchor_min_weight=_resolve(
            "MINIDEX_ANCHOR_MIN_WEIGHT",
            "anchor_min_weight",
            0.05,
            float,
            json_config,
        ),
    )
