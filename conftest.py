"""
pytest conftest.py — bootstrap the nix flake's pythonEnv site-packages.

When pytest is invoked outside of `nix develop` (e.g., directly from the
user's PATH), the active Python interpreter lacks project dependencies such
as pandas, requests, and pvlib.  This file discovers the flake's pythonEnv
derivation at collection time and inserts its site-packages into sys.path,
allowing `pytest test_awning_automation.py` to work from any shell.

If nix is unavailable or the evaluation fails, the path insertion is skipped
gracefully — in that case the caller is expected to activate `nix develop`
before running pytest.
"""

import json
import subprocess
import sys
from pathlib import Path

_FLAKE_DIR = str(Path(__file__).parent)

# Only inject if the required packages aren't already importable (i.e., we're
# NOT inside nix develop, which already has the right sys.path).
try:
    import pandas  # noqa: F401 — presence check only
except ImportError:
    try:
        # Determine the current system (aarch64-darwin, x86_64-linux, etc.)
        system_result = subprocess.run(
            ["nix", "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if system_result.returncode == 0:
            current_system = system_result.stdout.strip()
            # flake path and attribute must be a single argument: /path#attr
            flake_attr = f"{_FLAKE_DIR}#devShells.{current_system}.default.buildInputs"
            result = subprocess.run(
                [
                    "nix",
                    "eval",
                    flake_attr,
                    "--apply",
                    "builtins.map (x: x.outPath)",
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=_FLAKE_DIR,
            )
            if result.returncode == 0:
                store_paths = json.loads(result.stdout)
                for store_path in store_paths:
                    site_packages = (
                        Path(store_path) / "lib" / "python3.13" / "site-packages"
                    )
                    if site_packages.exists():
                        path_str = str(site_packages)
                        if path_str not in sys.path:
                            sys.path.insert(0, path_str)
                        break
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, OSError):
        # nix not available or evaluation failed — leave sys.path as-is
        pass
