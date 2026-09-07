"""Delegate dependency resolution to PDM; enforce only registry source policy."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from concurrent.futures import Future
from pathlib import Path
from threading import Lock
from typing import Protocol

import tomli_w
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

INDEX_URLS = {
    "pypi": "https://pypi.org/simple/",
    "testpypi": "https://test.pypi.org/simple/",
}
EXPORT_MAX_BYTES = 64 * 1024
EXPORT_MAX_COUNT = 512
RESOLUTION_TIMEOUT_SECONDS = 120.0
PYTHON_TARGETS = ("3.10", "3.11", "3.12", "3.13", "3.14")
# The concrete CI target, not an inferred lower bound for upstream wheel tags.
RESOLUTION_PLATFORM = "manylinux_2_39_x86_64"
_VANE_PACKAGES = ["vane-ai", "vane-extension-*"]


class ResolutionError(ValueError):
    """PDM could not produce a lock satisfying the registry's source policy."""


class InstallationResolver(Protocol):
    def resolve(
        self,
        *,
        distribution_name: str,
        version: str,
        requires_python: str,
        package_index: str,
    ) -> Mapping[str, tuple[str, ...]]: ...


def _is_vane_distribution(name: str) -> bool:
    return name == "vane-ai" or name.startswith("vane-extension-")


def _resolver_environment(root: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PIP_", "UV_", "PDM_", "PYTHON"))
    }
    environment.update(
        {
            "PDM_CONFIG_FILE": str(root / "pdm.toml"),
            "PDM_CACHE_DIR": str(root / "cache"),
            "PDM_LOG_DIR": str(root / "logs"),
            "PDM_CHECK_UPDATE": "false",
            "PDM_USE_UV": "false",
            "PDM_USE_VENV": "false",
            "PDM_IGNORE_STORED_INDEX": "true",
            "PDM_PYPI_JSON_API": "false",
            "PDM_ONLY_BINARY": ":all:",
            "PDM_PYTHON": sys.executable,
            "PDM_NON_INTERACTIVE": "true",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
            "NETRC": os.devnull,
        }
    )
    return environment


def _parse_export(
    contents: bytes,
    *,
    distribution_name: str,
    version: str,
    package_index: str,
) -> dict[str, tuple[str, ...]]:
    """Read PDM's requirements export without interpreting its dependency graph."""
    if len(contents) > EXPORT_MAX_BYTES:
        raise ResolutionError("dependency export exceeds its size limit")
    # PDM exports source directives too. Strip only these exact known lines;
    # pip recipes below use separate indexes instead of a mixed-index command.
    source_lines = {f"--index-url {INDEX_URLS['pypi']}"}
    if package_index == "testpypi":
        source_lines.add(f"--extra-index-url {INDEX_URLS['testpypi']}")
    result: dict[str, set[str]] = {index: set() for index in INDEX_URLS}
    root_requirements: list[Requirement] = []
    try:
        for line in contents.decode("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line in source_lines:
                continue
            requirement = Requirement(line)
            specifiers = list(requirement.specifier)
            if (
                requirement.url is not None
                or requirement.extras
                or len(specifiers) != 1
                or specifiers[0].operator not in {"==", "==="}
                or "*" in specifiers[0].version
            ):
                raise ResolutionError("export must contain only exact registry pins")
            Version(specifiers[0].version)
            name = canonicalize_name(requirement.name)
            index = package_index if _is_vane_distribution(name) else "pypi"
            if name == distribution_name:
                # Keep the requested provider unconditional so an unsupported
                # interpreter/platform fails at pip instead of silently skipping it.
                # PDM owns the merged target markers of every dependency.
                requirement.marker = None
                root_requirements.append(requirement)
            result[index].add(str(requirement))
        if sum(map(len, result.values())) > EXPORT_MAX_COUNT:
            raise ResolutionError("dependency export contains too many requirements")
        if len(root_requirements) != 1 or not root_requirements[0].specifier.contains(
            version, prereleases=True
        ):
            raise ResolutionError(
                "dependency export must include the unconditional provider"
            )
        return {index: tuple(sorted(pins)) for index, pins in result.items()}
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ResolutionError):
            raise
        raise ResolutionError("invalid PDM dependency export") from exc


class PdmInstallationResolver:
    """Resolve the provider and all dependencies together with PDM lock/export."""

    def __init__(self) -> None:
        self._inflight: dict[tuple[str, ...], Future[dict[str, tuple[str, ...]]]] = {}
        self._lock = Lock()

    def resolve(
        self,
        *,
        distribution_name: str,
        version: str,
        requires_python: str,
        package_index: str,
    ) -> Mapping[str, tuple[str, ...]]:
        key = (distribution_name, version, requires_python, package_index)
        with self._lock:
            future = self._inflight.get(key)
            owner = future is None
            if future is None:
                future = Future()
                self._inflight[key] = future
        if owner:
            try:
                future.set_result(
                    self._resolve_uncached(
                        distribution_name=distribution_name,
                        version=version,
                        requires_python=requires_python,
                        package_index=package_index,
                    )
                )
            except BaseException as exc:
                future.set_exception(exc)
                with self._lock:
                    self._inflight.pop(key, None)
                raise
        return dict(future.result())

    def _resolve_uncached(
        self,
        *,
        distribution_name: str,
        version: str,
        requires_python: str,
        package_index: str,
    ) -> dict[str, tuple[str, ...]]:
        sources = [{"name": "pypi", "url": INDEX_URLS["pypi"], "verify_ssl": True}]
        if package_index == "testpypi":
            sources[0]["exclude_packages"] = _VANE_PACKAGES
            sources.append(
                {
                    "name": "testpypi",
                    "url": INDEX_URLS["testpypi"],
                    "verify_ssl": True,
                    "include_packages": _VANE_PACKAGES,
                }
            )
        project = {
            "project": {
                "name": f"vane-registry-resolution-{secrets.token_hex(16)}",
                "version": "0",
                "requires-python": requires_python,
                "dependencies": [f"{distribution_name}==={version}"],
            },
            "tool": {"pdm": {"distribution": False, "source": sources}},
        }
        try:
            with tempfile.TemporaryDirectory(
                prefix="vane-installation-lock-"
            ) as temporary:
                root = Path(temporary)
                (root / "pyproject.toml").write_text(
                    tomli_w.dumps(project), encoding="utf-8"
                )
                (root / "pdm.toml").write_text(
                    tomli_w.dumps({"pypi": {"ignore_stored_index": True}}),
                    encoding="utf-8",
                )
                command = [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("pdm_runner.py")),
                    "--config",
                    str(root / "pdm.toml"),
                ]
                lock_commands = [
                    [
                        "lock",
                        *(
                            ["--append"]
                            if index
                            else ["--strategy", "inherit_metadata"]
                        ),
                        "--skip",
                        ":all",
                        "--python",
                        f"=={python}.*",
                        "--platform",
                        RESOLUTION_PLATFORM,
                        "--implementation",
                        "cpython",
                    ]
                    for index, python in enumerate(PYTHON_TARGETS)
                ]
                for arguments in (
                    *lock_commands,
                    [
                        "export",
                        "--no-hashes",
                        "--no-extras",
                        "--output",
                        "requirements.txt",
                    ],
                ):
                    result = subprocess.run(
                        [*command, *arguments],
                        cwd=root,
                        env=_resolver_environment(root),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=RESOLUTION_TIMEOUT_SECONDS,
                        check=False,
                    )
                    if result.returncode:
                        raise ResolutionError(
                            f"PDM {arguments[0]} failed for {distribution_name}"
                        )
                with (root / "requirements.txt").open("rb") as stream:
                    contents = stream.read(EXPORT_MAX_BYTES + 1)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ResolutionError(
                f"PDM resolution failed for {distribution_name}"
            ) from exc
        return _parse_export(
            contents,
            distribution_name=distribution_name,
            version=version,
            package_index=package_index,
        )
