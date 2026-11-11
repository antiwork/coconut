#!/usr/bin/env python3
"""Let the person's Hermes load the `coconut` skill, or stop it.

`add` puts the skill folder of this Coconut (`skills` in this folder) into `skills.external_dirs` of the default Hermes
profile and of each named profile in `<Hermes home>/profiles`, which are also the Coconut bots. `remove` takes it out
again. Setup runs `add` (deploy.py on a first install, setup_coconut.py), and the uninstaller runs `remove`. Coconut
changes the Hermes config only then. Each run is safe to repeat. backend/coconut/skill_dirs.py does the edit under the
Python of the person's Hermes. The same run installs or removes the `coconut` gateway platform
(backend/coconut/gateway_plugin.py).
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

import host


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"


def command(action: str, runtime) -> list[str]:
    """Each bot is a profile of the Hermes home, so the default home and its profiles cover every bot."""
    return [str(runtime.python), "-I", "-B", str(ROOT / "backend/coconut/skill_dirs.py"), action, str(runtime.root or ""),
            str(SKILLS), str(runtime.home)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["add", "remove"])
    args = parser.parse_args()
    # A team-only install never adds anything to Hermes, so it has nothing to add or remove there.
    refusal = host.team_folder_refusal()
    if refusal:
        print(refusal)
        return 1
    runtime_module = host.hermes_runtime()
    environment = host.service_environment()
    try:
        runtime = runtime_module.find(environment)
    except runtime_module.HermesUnavailable as error:
        print(f"Coconut did not change the Hermes settings: {error.reason}")
        return 1
    with runtime_module.isolated(environment) as (isolated, folder):
        # The edit runs in the real Hermes homes. The isolated environment only keeps the model keys out.
        # Earlier versions saved model keys in the data folder. The edit moves them into Hermes (model_keys.py).
        isolated = dict(isolated, COCONUT_SAVED_MODEL_KEYS=str(host.DEFAULT_DATA / "credentials.json"))
        result = subprocess.run(command(args.action, runtime), cwd=folder, env=isolated, stdin=subprocess.DEVNULL,
                                text=True, capture_output=True, timeout=120)
    sys.stdout.write(result.stdout)
    if result.returncode:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
