#!/usr/bin/env python
"""Install Hermes Power Guard into the active Hermes home without arming it."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_FILES = (
    "__init__.py",
    "power_guard_core.py",
    "plugin.yaml",
    "README.md",
    "README.zh-CN.md",
    "DESIGN.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "assets/hero.svg",
    "assets/ui-preview.svg",
    "docs/ARCHITECTURE.md",
    "docs/BENCHMARKS.md",
    "docs/COMPATIBILITY.md",
    "docs/PRODUCT-LANGUAGE.md",
    "docs/STATE-MATRIX.md",
    "docs/THREAT-MODEL.md",
    "docs/TROUBLESHOOTING.md",
    "desktop/plugin.js",
    "dashboard/manifest.json",
    "dashboard/plugin_api.py",
    "tests/test_core.py",
    "tests/test_api.py",
)


def hermes_home() -> Path:
    configured = os.getenv("HERMES_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        local = os.getenv("LOCALAPPDATA")
        if local:
            return (Path(local) / "hermes").resolve()
    return (Path.home() / ".hermes").resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="overlay known source files when the destination already exists",
    )
    args = parser.parse_args()

    source = Path(__file__).resolve().parents[1]
    destination = hermes_home() / "plugins" / "power-guard"

    same_tree = source.resolve() == destination.resolve()
    if destination.exists() and not same_tree and not args.update:
        print(f"Refusing to overwrite existing plugin: {destination}", file=sys.stderr)
        print("Re-run with --update to overlay known source files.", file=sys.stderr)
        return 2

    if not same_tree:
        for relative in PLUGIN_FILES:
            src = source / relative
            dst = destination / relative
            if not src.is_file():
                print(f"Missing release file: {src}", file=sys.stderr)
                return 2
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    command = ["hermes", "plugins", "enable", "power-guard", "--no-allow-tool-override"]
    try:
        subprocess.run(command, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"Plugin files installed at {destination}", file=sys.stderr)
        print(f"Could not enable through Hermes CLI: {exc}", file=sys.stderr)
        print("Run: hermes plugins enable power-guard --no-allow-tool-override", file=sys.stderr)
        return 1

    print(f"Installed Power Guard at {destination}")
    print("It is DISARMED. Restart Hermes Desktop once, then enable Power Guard in Settings -> Plugins.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
