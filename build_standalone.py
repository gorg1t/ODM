"""Build a standalone application bundle with PyInstaller."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
SPEC_PATH = ROOT_DIR / "onvif_ptz_controller.spec"
APP_NAME = "onvif-ptz-controller"
LEGACY_DIST_DIR = ROOT_DIR / "dist"
LEGACY_BUILD_DIR = ROOT_DIR / "build"
PYINSTALLER_DIR = ROOT_DIR / ".pyinstaller"
WORK_DIR = PYINSTALLER_DIR / "build"
RELEASE_DIR = ROOT_DIR / "release"


def _platform_tag() -> str:
    system_name = platform.system().lower()
    machine_name = platform.machine().lower()
    machine_aliases = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    normalized_machine = machine_aliases.get(machine_name, machine_name or "unknown")
    return f"{system_name}-{normalized_machine}"


PLATFORM_TAG = _platform_tag()
DIST_DIR = RELEASE_DIR / PLATFORM_TAG
ARCHIVE_BASE = RELEASE_DIR / f"{APP_NAME}-{PLATFORM_TAG}"
ARCHIVE_PATH = ARCHIVE_BASE.with_suffix(".zip")
BUNDLE_DIR = DIST_DIR / APP_NAME


def _remove_existing_outputs() -> None:
    for path in (LEGACY_DIST_DIR, LEGACY_BUILD_DIR, DIST_DIR, WORK_DIR):
        if path.exists():
            shutil.rmtree(path)
    if ARCHIVE_PATH.exists():
        ARCHIVE_PATH.unlink()


def _ensure_parent_directories() -> None:
    DIST_DIR.parent.mkdir(parents=True, exist_ok=True)
    WORK_DIR.parent.mkdir(parents=True, exist_ok=True)


def _create_archive() -> None:
    shutil.make_archive(
        str(ARCHIVE_BASE),
        "zip",
        root_dir=DIST_DIR,
        base_dir=APP_NAME,
    )


def main() -> int:
    if not SPEC_PATH.exists():
        print(f"Spec file not found: {SPEC_PATH}", file=sys.stderr)
        return 1

    _remove_existing_outputs()
    _ensure_parent_directories()

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(WORK_DIR),
        str(SPEC_PATH),
    ]

    print("Building standalone bundle...")
    print("Command:", " ".join(command))
    subprocess.run(command, cwd=ROOT_DIR, check=True)
    _create_archive()

    print(f"Standalone bundle created in: {BUNDLE_DIR}")
    print(f"Archive created in: {ARCHIVE_PATH}")
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())