"""Domain packs: the persona, rules and tuning the AI assistant runs with.

Each domain is a folder next to this file:

    <name>/domain.toml   label, temperature, retrieval tuning (all optional)
    <name>/system.md     system instruction sent on every call (required)
    <name>/canon/*.md    optional — always included in the system instruction
                         (knowledge the model must never lose, e.g. a style
                         guide or a world bible), in file-name order

Picked by APP_DOMAIN (default "research"). A domain folder that is missing —
e.g. one kept out of Git via .gitignore — falls back to "research" with a
warning instead of crashing, so a fresh clone always starts.

Loaded once per process and cached: edit a .md file, restart the server.
"""
import logging
import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("uvicorn.error")

_DOMAINS_DIR = Path(__file__).resolve().parent
DEFAULT_DOMAIN = "research"


@dataclass(frozen=True)
class Domain:
    name: str
    label: str
    system: str
    temperature: float
    chunk_characters: int
    chunk_overlap: int
    top_k: int
    # Total extracted characters at or below which every chunk is sent (no
    # retrieval at all): with a handful of short documents, the model reading
    # everything beats any keyword ranking.
    full_text_max_chars: int
    # Retrieval drops chunks scoring below this fraction of the best chunk's
    # score, so a weak one-word match doesn't pad the context with noise.
    min_relative_score: float


def _read_canon(folder: Path) -> str:
    canon_dir = folder / "canon"
    if not canon_dir.is_dir():
        return ""
    parts = []
    for path in sorted(canon_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if text:
            parts.append(f"----- {path.stem} -----\n{text}")
    return "\n\n".join(parts)


def _load(name: str) -> Domain:
    folder = _DOMAINS_DIR / name
    config_path = folder / "domain.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    retrieval = config.get("retrieval", {})

    system = (folder / "system.md").read_text(encoding="utf-8").strip()
    canon = _read_canon(folder)
    if canon:
        system += "\n\n=== CANON (luôn đúng, ưu tiên hơn mọi nguồn khác) ===\n\n" + canon

    return Domain(
        name=name,
        label=config.get("label", name),
        system=system,
        temperature=float(config.get("temperature", 0.2)),
        chunk_characters=int(retrieval.get("chunk_characters", 1200)),
        chunk_overlap=int(retrieval.get("chunk_overlap", 150)),
        top_k=int(retrieval.get("top_k", 8)),
        full_text_max_chars=int(retrieval.get("full_text_max_chars", 120_000)),
        min_relative_score=float(retrieval.get("min_relative_score", 0.25)),
    )


@lru_cache(maxsize=None)
def _cached(name: str) -> Domain:
    if not (_DOMAINS_DIR / name / "system.md").exists():
        if name == DEFAULT_DOMAIN:
            raise RuntimeError(f"Domain mặc định '{DEFAULT_DOMAIN}' thiếu system.md")
        log.warning("domain_missing name=%s fallback=%s", name, DEFAULT_DOMAIN)
        return _cached(DEFAULT_DOMAIN)
    return _load(name)


def current() -> Domain:
    name = os.environ.get("APP_DOMAIN", "").strip().lower() or DEFAULT_DOMAIN
    return _cached(name)
