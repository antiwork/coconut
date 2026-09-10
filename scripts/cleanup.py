#!/usr/bin/env python3
"""Remove this project's Hermes runtime and local service data."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
DEPLOYMENT_FILES = {'ssh', 'remote.json', 'iphone.json', 'iphone.lock'}


def stop_for_cleanup() -> Path:
    import host
    host.stop_for_update()
    path, loaded = host.owned_agent()
    if loaded:
        raise SystemExit("Coconut's LaunchAgent restarted during cleanup. Cleanup kept its files intact.")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Delete the local runtime after stopping its service.")
    parser.add_argument("--builds", action="store_true", help="Also delete local Xcode build output.")
    parser.add_argument("--keep-deployment", action="store_true",
                        help="Keep Mac mini SSH access and iPhone update settings while removing local execution and data.")
    args = parser.parse_args()
    if RUNTIME.is_symlink():
        raise SystemExit("The runtime is a symbolic link. Cleanup did not follow it.")
    targets = [RUNTIME]
    if args.keep_deployment:
        targets = [path for path in sorted(RUNTIME.iterdir()) if path.name not in DEPLOYMENT_FILES] if RUNTIME.exists() else []
        if RUNTIME / 'hermes' not in targets:
            targets.append(RUNTIME / 'hermes')
    if args.builds:
        targets.extend([ROOT / "ios/.build", ROOT / "ios/DerivedData", ROOT / ".build"])
    for target in targets:
        print(f"Remove: {target}")
    if not args.yes:
        print("This preview changed nothing. Add --yes to delete these files.")
        return
    for target in targets:
        if target.is_symlink():
            raise SystemExit(f"Cleanup did not follow a symbolic link: {target}")
    import terminal
    with terminal.runtime_lease(exclusive=True):
        plist = stop_for_cleanup()
        terminal.uninstall_command()
        if plist.exists():
            plist.unlink()
        for target in targets:
            if target.is_symlink():
                raise SystemExit(f"Cleanup did not follow a symbolic link: {target}")
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
    print("Removed the local Hermes runtime and Coconut data. The source files remain.")
    if args.keep_deployment:
        print("Kept Mac mini SSH access and iPhone update settings.")


if __name__ == "__main__":
    main()
