"""Shared helpers: logging setup, time/path utilities, and the canonical
ffmpeg wrapper. Every subprocess call in the codebase goes through
`run_ffmpeg()` so flags, timeout handling, and error capture stay uniform.
"""

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_ffmpeg(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Run ffmpeg synchronously, return CompletedProcess. Never shell=True."""
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def relative_to_data(p: Path, data_dir: Path) -> str:
    try:
        return str(p.relative_to(data_dir))
    except ValueError:
        return str(p)


def first_existing(*paths: Path) -> Path | None:
    for p in paths:
        if p and p.exists():
            return p
    return None
