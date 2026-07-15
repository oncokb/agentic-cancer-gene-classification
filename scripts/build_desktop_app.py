"""Build a one-file executable for the browser UI launcher."""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NAME = "GeneFusionAnnotator"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build an OS-specific executable that starts the local UI and opens a browser."
        )
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_NAME,
        help=f"Executable/app name (default: {DEFAULT_NAME}).",
    )
    parser.add_argument(
        "--windowed",
        action="store_true",
        help=(
            "Hide the console window where supported. Use console mode first if you "
            "need visible logs for troubleshooting."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_separator = ";" if platform.system() == "Windows" else ":"
    static_data = f"{ROOT / 'src' / 'static'}{data_separator}src/static"

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--onefile",
        "--name",
        args.name,
        "--add-data",
        static_data,
    ]
    if args.windowed:
        command.append("--windowed")
    command.append(str(ROOT / "src" / "ui_launcher.py"))

    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
