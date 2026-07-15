"""Local app configuration storage for UI-provided credentials."""

from __future__ import annotations

import os
import platform
from pathlib import Path


def app_config_dir() -> Path:
    override = os.environ.get("AGCG_CONFIG_DIR")
    if override:
        return Path(override)
    if platform.system() == "Windows":
        app_data = os.environ.get("APPDATA")
        return (
            Path(app_data) / "AgenticCancerGeneClassification"
            if app_data
            else Path.home()
        )
    if platform.system() == "Darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "AgenticCancerGeneClassification"
        )
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "agentic-cancer-gene-classification"
    )


def write_secret_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
