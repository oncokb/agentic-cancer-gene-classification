"""Build distributable artifacts for the browser UI launcher."""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NAME = "GeneFusionAnnotator"


def _artifact_name(name: str) -> str:
    system = platform.system().lower()
    machine = platform.machine().lower() or "unknown"
    return f"{name}-{system}-{machine}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build OS-specific desktop artifacts that start the local UI and "
            "open a browser."
        )
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_NAME,
        help=f"Executable/app name (default: {DEFAULT_NAME}).",
    )
    parser.add_argument(
        "--format",
        choices=("executable", "mac-app", "dmg"),
        default="executable",
        help=(
            "Artifact type to build. 'mac-app' and 'dmg' are only supported on "
            "macOS. Default: executable."
        ),
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


def build_with_pyinstaller(args: argparse.Namespace) -> None:
    data_separator = ";" if platform.system() == "Windows" else ":"
    static_data = f"{ROOT / 'src' / 'static'}{data_separator}src/static"
    mac_bundle = args.format in {"mac-app", "dmg"}

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--name",
        args.name,
        "--add-data",
        static_data,
    ]
    if mac_bundle:
        command.append("--onedir")
    else:
        command.append("--onefile")
    if args.windowed or mac_bundle:
        command.append("--windowed")
    command.append(str(ROOT / "src" / "ui_launcher.py"))

    subprocess.run(command, cwd=ROOT, check=True)


def write_dmg_readme(staging_dir: Path, app_name: str) -> None:
    readme = staging_dir / "README.txt"
    readme.write_text(
        f"""\
{app_name} internal beta

1. Drag {app_name}.app into Applications.
2. Open Applications and launch {app_name}.
3. On first launch, macOS may require right-clicking the app and choosing Open.
4. Keep the launcher running while using the browser UI.

The app stores user-entered tokens in the local user environment/configuration.
API keys are not bundled into this disk image.
""",
        encoding="utf-8",
    )


def build_dmg(name: str) -> Path:
    if platform.system() != "Darwin":
        raise SystemExit("--format dmg can only be built on macOS.")

    app_path = ROOT / "dist" / f"{name}.app"
    if not app_path.exists():
        raise SystemExit(f"Expected app bundle not found: {app_path}")

    artifact_name = _artifact_name(name)
    staging_dir = ROOT / "dist" / f"{artifact_name}-dmg-root"
    dmg_path = ROOT / "dist" / f"{artifact_name}.dmg"

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)
    shutil.copytree(app_path, staging_dir / app_path.name)
    (staging_dir / "Applications").symlink_to("/Applications")
    write_dmg_readme(staging_dir, name)

    subprocess.run(
        [
            "hdiutil",
            "create",
            "-volname",
            name,
            "-srcfolder",
            str(staging_dir),
            "-ov",
            "-format",
            "UDZO",
            str(dmg_path),
        ],
        cwd=ROOT,
        check=True,
    )
    shutil.rmtree(staging_dir)
    return dmg_path


def main() -> None:
    args = parse_args()
    if args.format in {"mac-app", "dmg"} and platform.system() != "Darwin":
        raise SystemExit(f"--format {args.format} can only be built on macOS.")

    build_with_pyinstaller(args)
    if args.format == "dmg":
        dmg_path = build_dmg(args.name)
        print(f"Built {dmg_path}")


if __name__ == "__main__":
    main()
