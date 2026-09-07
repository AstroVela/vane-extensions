"""Exercise the generated POSIX recipes in fresh environments on the CI runner.

Run with the target Python (stdlib only), after building and validating the site.
This verifies the actual runner, not all platforms described by upstream tags.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import tempfile
import venv
from pathlib import Path

_LOAD_CHECK = """
import sys
import vane

with vane.connect(":memory:", config={
    "autoinstall_known_extensions": "false",
    "autoload_known_extensions": "false",
}) as connection:
    vane.load_installed_extension(sys.argv[1], connection=connection)
    loaded = connection.execute(
        "SELECT loaded FROM duckdb_extensions() WHERE extension_name = ?",
        [sys.argv[1]],
    ).fetchone()
    if loaded != (True,):
        raise RuntimeError(f"extension is not loaded: {sys.argv[1]}")
"""


def smoke_install(site: Path) -> int:
    aggregate = json.loads(
        (site / "v1/extensions/index.json").read_text(encoding="utf-8")
    )
    checked = 0
    for entry in aggregate["extensions"]:
        if not entry["package"]["published"]:
            continue
        name = entry["extension_name"]
        version = entry["package"]["latest_version"]
        script = entry["installation"]["posix_install_script"]
        if not isinstance(script, str) or not script.strip():
            raise ValueError(f"published provider has no installation recipe: {name}")
        print(
            f"Checking {name}=={version} on Python {platform.python_version()} / {platform.platform()}",
            flush=True,
        )
        with tempfile.TemporaryDirectory(prefix="vane-registry-smoke-") as temporary:
            root = Path(temporary)
            environment = root / "venv"
            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / "bin/python"
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.upper().startswith(("PIP_", "UV_", "PYTHON"))
            }
            env["PATH"] = (
                f"{environment / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
            )
            subprocess.run(
                ["bash", "-e", "-c", script], cwd=root, env=env, check=True, timeout=600
            )
            subprocess.run(
                [str(python), "-I", "-c", _LOAD_CHECK, name],
                cwd=root,
                env=env,
                check=True,
                timeout=120,
            )
        checked += 1
        print(
            f"Installed, dependency-checked, and loaded {name}=={version}", flush=True
        )
    print(f"Verified {checked} published providers on this runner", flush=True)
    return checked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    smoke_install(parser.parse_args().site)


if __name__ == "__main__":
    main()
