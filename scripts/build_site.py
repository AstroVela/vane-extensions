"""Build the enriched static registry site from reviewed manifests and live metadata."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from copy import copy
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import NoReturn, Protocol
from urllib.parse import quote, urlsplit

import httpx
import tomli_w
from dep_logic.markers import (
    AnyMarker,
    BaseMarker,
    EmptyMarker,
    MarkerUnion,
    MultiMarker,
    from_pkg_marker,
)
from dep_logic.markers.utils import intersection
from dep_logic.specifiers import RangeSpecifier, UnionSpecifier, from_specifierset
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import (
    InvalidTag,
    Tag,
    TooManyTagsError,
    cpython_tags,
    mac_platforms,
    parse_tag,
)
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from scripts.build_catalog import (
    DEFAULT_MANIFEST_ROOT,
    PROJECT_ROOT,
    catalog_bytes,
    load_manifests,
)

DETAIL_FORMAT_VERSION = 1
DETAIL_MAX_JSON_BYTES = 1024 * 1024
AGGREGATE_MAX_JSON_BYTES = 8 * 1024 * 1024
REMOTE_METADATA_MAX_BYTES = 8 * 1024 * 1024
REMOTE_METADATA_TIMEOUT_SECONDS = 15.0
METADATA_MAX_WORKERS = 16
PACKAGE_REQUIREMENTS_MAX_COUNT = 256
PACKAGE_WHEEL_TAGS_MAX_COUNT = 512
WHEEL_CLOSURE_MAX_STEPS = 100_000
VANE_REQUIREMENTS_MAX_COUNT = 64
PUBLIC_LOCK_MAX_BYTES = 64 * 1024
PUBLIC_LOCK_MAX_COUNT = 512
PUBLIC_LOCK_TIMEOUT_SECONDS = 120.0
INSTALL_SCRIPT_MAX_LENGTH = 16 * 1024
_PYTHON_TAG_RE = re.compile(r"^(cp|pp|py)([0-9])([0-9]+)$")
_BROAD_PYTHON_TAG_RE = re.compile(r"^py([0-9])$")
_VERSIONED_INTERPRETER_TAG_RE = re.compile(
    r"^([a-z][a-z0-9_]*?)([0-9])([0-9]+)$"
)
_CPYTHON_ABI_RE = re.compile(r"cp([0-9])([0-9]+)(t?)(d?)(m?)(u?)")
_PYPY_ABI_RE = re.compile(r"pypy([0-9])([0-9]+)_pp[0-9]+")
_LINUX_PLATFORM_RE = re.compile(
    r"^(manylinux(?:_[0-9]+_[0-9]+|1|2010|2014)|"
    r"musllinux_[0-9]+_[0-9]+|linux)_(.+)$"
)
_MACOS_PLATFORM_RE = re.compile(r"^macosx_([0-9]+)_([0-9]+)_(.+)$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)
_PACKAGE_INDEXES = {
    "pypi": {
        "api": "https://pypi.org/pypi/{distribution}/json",
        "release_api": "https://pypi.org/pypi/{distribution}/{version}/json",
        "project": "https://pypi.org/project/{distribution}/",
        "simple": "https://pypi.org/simple/",
    },
    "testpypi": {
        "api": "https://test.pypi.org/pypi/{distribution}/json",
        "release_api": "https://test.pypi.org/pypi/{distribution}/{version}/json",
        "project": "https://test.pypi.org/project/{distribution}/",
        "simple": "https://test.pypi.org/simple/",
    },
}
_PIP_COMMAND = (
    "python",
    "-m",
    "pip",
    "--isolated",
)
_TEMPLATE_ENVIRONMENT = Environment(
    loader=FileSystemLoader(PROJECT_ROOT / "site"),
    autoescape=True,
    keep_trailing_newline=True,
    undefined=StrictUndefined,
)


class SiteBuildError(ValueError):
    """The enriched site could not be generated safely."""


def _fail(message: str) -> NoReturn:
    raise SiteBuildError(message)


class JsonMetadataClient(Protocol):
    """Minimal interface used by the metadata enrichment pipeline."""

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> object | None: ...


class PublicDependencyResolver(Protocol):
    """Resolve an exact wheel closure from the public PyPI index."""

    def resolve(
        self,
        requirements: tuple[str, ...],
        *,
        requires_python: str,
        distribution_name: str,
        reject_vane: bool,
    ) -> tuple[str, ...]: ...


class MetadataClient:
    """Strict, bounded client for fixed GitHub and Python-index JSON endpoints."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "vane-extension-registry/1",
            },
            limits=httpx.Limits(
                max_connections=METADATA_MAX_WORKERS,
                max_keepalive_connections=METADATA_MAX_WORKERS,
            ),
            timeout=httpx.Timeout(REMOTE_METADATA_TIMEOUT_SECONDS),
            transport=transport,
        )

    def __enter__(self) -> MetadataClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> object | None:
        try:
            with self._client.stream("GET", url, headers=headers) as response:
                if allow_not_found and response.status_code == 404:
                    return None
                if str(response.url) != url or response.status_code != 200:
                    _fail(
                        "metadata endpoint returned "
                        f"HTTP {response.status_code} for {url}"
                    )
                contents = bytearray()
                for chunk in response.iter_bytes():
                    contents.extend(chunk)
                    if len(contents) > REMOTE_METADATA_MAX_BYTES:
                        _fail(f"metadata response exceeds its size limit for {url}")
        except SiteBuildError:
            raise
        except (httpx.HTTPError, OSError) as exception:
            raise SiteBuildError(f"could not fetch metadata from {url}") from exception
        try:
            return json.loads(contents.decode("utf-8"), object_pairs_hook=_unique_object)
        except SiteBuildError:
            raise
        except (UnicodeError, ValueError, RecursionError) as exception:
            raise SiteBuildError(
                f"metadata endpoint did not return valid UTF-8 JSON for {url}"
            ) from exception


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"metadata JSON repeats key {key!r}")
        result[key] = value
    return result


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(f"{field} must be an object")
    return value


def _string(value: object, field: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"{field} must be a non-empty trimmed string")
    if len(value) > 4096 or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        _fail(f"{field} contains invalid text")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _fail(f"{field} must not contain lone Unicode surrogates")
    return value


def _timestamp(value: str, field: str) -> datetime:
    if not _UTC_TIMESTAMP_RE.fullmatch(value):
        _fail(f"{field} must be a UTC RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exception:
        raise SiteBuildError(f"{field} must be a UTC RFC 3339 timestamp") from exception
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail(f"{field} must be a UTC RFC 3339 timestamp")
    return parsed


def _github_slug(repository: str) -> str:
    parsed = urlsplit(repository)
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname != "github.com" or len(parts) != 2 or parts[1].endswith(".git"):
        _fail(f"repository must identify one canonical GitHub repository: {repository}")
    return "/".join(parts)


def _github_metadata(
    repository: str, client: JsonMetadataClient, github_token: str | None
) -> dict[str, object]:
    slug = _github_slug(repository)
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    value = client.get_json(
        f"https://api.github.com/repos/{quote(slug, safe='/')}", headers=headers
    )
    document = _mapping(value, f"GitHub metadata for {slug}")
    full_name = _string(document.get("full_name"), "GitHub full_name")
    html_url = _string(document.get("html_url"), "GitHub html_url")
    stars = document.get("stargazers_count")
    if (
        not isinstance(full_name, str)
        or full_name.casefold() != slug.casefold()
        or html_url != repository
    ):
        _fail(f"GitHub metadata identity does not match {repository}")
    if type(stars) is not int or stars < 0:
        _fail(f"GitHub stargazers_count is invalid for {repository}")
    return {"github_stars": stars}


def _python_version_from_tag(interpreter: str) -> str | None:
    match = _PYTHON_TAG_RE.fullmatch(interpreter)
    if match is None:
        return None
    return f"{match.group(2)}.{int(match.group(3))}"


def _package_requirements(
    info: Mapping[str, object], distribution_name: str
) -> tuple[Requirement, ...]:
    raw_requirements = info.get("requires_dist")
    if raw_requirements is None:
        return ()
    if (
        not isinstance(raw_requirements, list)
        or len(raw_requirements) > PACKAGE_REQUIREMENTS_MAX_COUNT
    ):
        _fail(f"package requirements are invalid for {distribution_name}")
    requirements: dict[str, Requirement] = {}
    for raw_requirement in raw_requirements:
        requirement_text = _string(raw_requirement, "package requirement")
        if not isinstance(requirement_text, str):
            _fail(f"package requirement is missing for {distribution_name}")
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exception:
            raise SiteBuildError(
                f"package requirement is invalid for {distribution_name}"
            ) from exception
        normalized = str(requirement)
        requirements[normalized] = requirement
    return tuple(requirements[key] for key in sorted(requirements, key=str.casefold))


def _package_file_metadata(
    document: Mapping[str, object],
    distribution_name: str,
    version_text: str,
    version: Version,
) -> tuple[dict[str, object], frozenset[Tag]]:
    raw_files = document.get("urls")
    if not isinstance(raw_files, list):
        _fail(f"package urls must be a list for {distribution_name}")
    wheel_count = 0
    python_versions: set[str] = set()
    python_tags: set[str] = set()
    abi_tags: set[str] = set()
    platform_tags: set[str] = set()
    available_wheel_tags: set[Tag] = set()
    upload_times: list[tuple[datetime, str]] = []
    for raw_file in raw_files:
        package_file = _mapping(raw_file, f"package file for {distribution_name}")
        package_type = _string(
            package_file.get("packagetype"), "package packagetype"
        )
        yanked = package_file.get("yanked", False)
        if type(yanked) is not bool:
            _fail(f"package yanked flag is invalid for {distribution_name}")
        uploaded_at = _string(
            package_file.get("upload_time_iso_8601"),
            "package upload_time_iso_8601",
            nullable=True,
        )
        if uploaded_at is not None:
            upload_times.append(
                (
                    _timestamp(uploaded_at, "package upload_time_iso_8601"),
                    uploaded_at,
                )
            )
        if package_type != "bdist_wheel" or yanked:
            continue
        filename = _string(package_file.get("filename"), "package filename")
        if not isinstance(filename, str):
            _fail(f"package filename is missing for {distribution_name}")
        try:
            # Bound compressed tag expansion before parsing the full filename.
            parse_tag(
                "-".join(filename.removesuffix(".whl").rsplit("-", 3)[-3:]),
                limit=PACKAGE_WHEEL_TAGS_MAX_COUNT,
            )
            wheel_name, wheel_version, _build, parsed_tags = parse_wheel_filename(
                filename
            )
        except (InvalidWheelFilename, InvalidTag, TooManyTagsError) as exception:
            raise SiteBuildError(
                f"package returned an invalid wheel filename for {distribution_name}"
            ) from exception
        if (
            canonicalize_name(wheel_name) != canonicalize_name(distribution_name)
            or wheel_version != version
        ):
            _fail(f"wheel identity does not match {distribution_name}=={version_text}")
        wheel_count += 1
        available_wheel_tags.update(parsed_tags)
        if len(available_wheel_tags) > PACKAGE_WHEEL_TAGS_MAX_COUNT:
            _fail(f"package has too many wheel tags for {distribution_name}")
        for tag in parsed_tags:
            python_tags.add(tag.interpreter)
            abi_tags.add(tag.abi)
            platform_tags.add(tag.platform)
            python_version = _python_version_from_tag(tag.interpreter)
            if python_version is not None:
                python_versions.add(python_version)

    latest_release_uploaded_at = (
        max(upload_times, key=lambda item: item[0])[1] if upload_times else None
    )
    return (
        {
            "wheel_count": wheel_count,
            "python_versions": sorted(python_versions, key=Version),
            "python_tags": sorted(python_tags),
            "abi_tags": sorted(abi_tags),
            "platform_tags": sorted(platform_tags),
            "latest_release_uploaded_at": latest_release_uploaded_at,
        },
        frozenset(available_wheel_tags),
    )


def _package_metadata(
    distribution_name: str, package_index: str, client: JsonMetadataClient
) -> tuple[dict[str, object], tuple[Requirement, ...], frozenset[Tag]]:
    index = _PACKAGE_INDEXES[package_index]
    escaped_distribution = quote(distribution_name, safe="-")
    api_url = index["api"].format(distribution=escaped_distribution)
    project_url = index["project"].format(distribution=escaped_distribution)
    value = client.get_json(api_url, allow_not_found=True)
    if value is None:
        return (
            {
                "index": package_index,
                "project_url": project_url,
                "published": False,
                "latest_version": None,
                "requires_python": None,
                "requires_dist": [],
                "wheel_count": 0,
                "python_versions": [],
                "python_tags": [],
                "abi_tags": [],
                "platform_tags": [],
                "latest_release_uploaded_at": None,
            },
            (),
            frozenset(),
        )

    document = _mapping(value, f"package metadata for {distribution_name}")
    info = _mapping(document.get("info"), f"package info for {distribution_name}")
    reported_name = _string(info.get("name"), "package info.name")
    version_text = _string(info.get("version"), "package info.version")
    if (
        not isinstance(reported_name, str)
        or canonicalize_name(reported_name) != canonicalize_name(distribution_name)
    ):
        _fail(f"package metadata identity does not match {distribution_name}")
    if not isinstance(version_text, str):
        _fail(f"package version is missing for {distribution_name}")
    try:
        latest_version = Version(version_text)
    except InvalidVersion as exception:
        raise SiteBuildError(
            f"package version is invalid for {distribution_name}"
        ) from exception

    requires_python = _string(
        info.get("requires_python"), "package info.requires_python", nullable=True
    )
    if requires_python is not None:
        try:
            SpecifierSet(requires_python)
        except InvalidSpecifier as exception:
            raise SiteBuildError(
                f"package Requires-Python is invalid for {distribution_name}"
            ) from exception
    requirements = _package_requirements(info, distribution_name)
    file_metadata, wheel_tags = _package_file_metadata(
        document, distribution_name, version_text, latest_version
    )
    return (
        {
            "index": package_index,
            "project_url": project_url,
            "published": True,
            "latest_version": version_text,
            "requires_python": requires_python,
            "requires_dist": [
                str(requirement)
                for requirement in requirements
                if requirement.url is None
            ],
            **file_metadata,
        },
        requirements,
        wheel_tags,
    )


def _download_metadata(
    distribution_name: str,
    package_index: str,
    published: bool,
    client: JsonMetadataClient,
) -> dict[str, object]:
    if package_index != "pypi" or not published:
        return {"downloads_last_week": None, "source": None}
    url = f"https://pypistats.org/api/packages/{quote(distribution_name, safe='-')}/recent"
    value = client.get_json(url, allow_not_found=True)
    if value is None:
        return {"downloads_last_week": None, "source": None}
    document = _mapping(value, f"download metadata for {distribution_name}")
    data = _mapping(document.get("data"), f"download data for {distribution_name}")
    downloads = data.get("last_week")
    if type(downloads) is not int or downloads < 0:
        _fail(f"download count is invalid for {distribution_name}")
    return {"downloads_last_week": downloads, "source": "pypistats.org"}


def _is_vane_distribution(distribution_name: str) -> bool:
    normalized = canonicalize_name(distribution_name)
    return normalized == "vane-ai" or normalized.startswith("vane-extension-")


def _marker_for_base_install(marker: BaseMarker) -> BaseMarker:
    """Substitute an empty selected extra while preserving other conditions."""
    if marker.is_any() or marker.is_empty():
        return marker
    if isinstance(marker, MultiMarker):
        return MultiMarker.of(*(_marker_for_base_install(item) for item in marker))
    if isinstance(marker, MarkerUnion):
        return MarkerUnion.of(*(_marker_for_base_install(item) for item in marker))
    if marker.only("extra") != marker:
        return marker
    if Marker(str(marker)).evaluate(
        environment={"extra": ""}, context="metadata"
    ):
        return AnyMarker()
    return EmptyMarker()


def _requirement_for_base_install(requirement: Requirement) -> Requirement | None:
    """Apply the empty-extra context without evaluating platform conditions."""
    if requirement.marker is None:
        return requirement
    try:
        marker = from_pkg_marker(requirement.marker)
        base_marker = _marker_for_base_install(marker)
        if base_marker.is_empty():
            return None
        if base_marker.is_any():
            base_requirement = copy(requirement)
            base_requirement.marker = None
            return base_requirement
        if base_marker == marker:
            return requirement
        base_requirement = copy(requirement)
        base_requirement.marker = Marker(str(base_marker))
        return base_requirement
    except (KeyError, TypeError, ValueError) as exception:
        raise SiteBuildError(
            "package marker cannot be evaluated for "
            f"{canonicalize_name(requirement.name)}"
        ) from exception


def _python_environment_marker(
    requires_python: object, distribution_name: str
) -> BaseMarker:
    if requires_python is None:
        return AnyMarker()
    if not isinstance(requires_python, str):
        _fail(f"package Requires-Python is invalid for {distribution_name}")
    try:
        specifiers = SpecifierSet(requires_python)
        markers = tuple(
            from_pkg_marker(
                Marker(
                    f'python_full_version {specifier.operator} '
                    f'"{specifier.version}"'
                )
            )
            for specifier in specifiers
        )
        environment = MultiMarker.of(*markers) if markers else AnyMarker()
    except (KeyError, TypeError, ValueError) as exception:
        raise SiteBuildError(
            f"package Requires-Python is invalid for {distribution_name}"
        ) from exception
    if environment.is_empty():
        _fail(f"package Requires-Python is empty for {distribution_name}")
    return environment


def _equals_marker(name: str, value: str) -> BaseMarker:
    return from_pkg_marker(Marker(f"{name} == {json.dumps(value)}"))


def _one_of_marker(name: str, values: frozenset[str]) -> BaseMarker:
    return MarkerUnion.of(*(_equals_marker(name, value) for value in values))


def _implementation_environment(interpreter: str) -> BaseMarker:
    implementations = {
        "cp": ("cpython", "CPython"),
        "graalpy": ("graalpy", "GraalPy"),
        "ip": ("ironpython", "IronPython"),
        "jy": ("jython", "Jython"),
        "pp": ("pypy", "PyPy"),
    }
    names = implementations.get(interpreter)
    if names is None:
        return _equals_marker("implementation_name", interpreter)
    implementation_name, display_name = names
    return MultiMarker.of(
        _equals_marker("implementation_name", implementation_name),
        _equals_marker("platform_python_implementation", display_name),
    )


def _wheel_python_environment(tag: Tag) -> BaseMarker:
    if tag.platform == "any" and tag.abi != "none":
        return EmptyMarker()
    broad_match = _BROAD_PYTHON_TAG_RE.fullmatch(tag.interpreter)
    if broad_match is not None:
        if tag.abi != "none":
            return EmptyMarker()
        major = int(broad_match.group(1))
        return MultiMarker.of(
            from_pkg_marker(Marker(f'python_version >= "{major}"')),
            from_pkg_marker(Marker(f'python_version < "{major + 1}"')),
        )

    versioned_match = _VERSIONED_INTERPRETER_TAG_RE.fullmatch(tag.interpreter)
    if versioned_match is None:
        return EmptyMarker()
    interpreter, major_text, minor_text = versioned_match.groups()
    major = int(major_text)
    minor = int(minor_text)
    version = f"{major}.{minor}"
    if interpreter == "cp" and tag.abi not in {"none", "abi3", "abi3t"}:
        abi_match = _CPYTHON_ABI_RE.fullmatch(tag.abi)
        if abi_match is None:
            return EmptyMarker()
        abi_major, abi_minor, threaded, _debug, pymalloc, ucs4 = abi_match.groups()
        if (
            (int(abi_major), int(abi_minor)) != (major, minor)
            or (threaded and (major, minor) < (3, 13))
            or (pymalloc and (major, minor) >= (3, 8))
            or (ucs4 and (major, minor) >= (3, 3))
        ):
            return EmptyMarker()
    elif interpreter != "cp" and tag.abi != "none":
        # Only admit native ABIs whose interpreter identity we can validate.
        # Matching two arbitrary ABI strings does not make either installable.
        if interpreter != "pp":
            return EmptyMarker()
        abi_match = _PYPY_ABI_RE.fullmatch(tag.abi)
        if abi_match is None or tuple(map(int, abi_match.groups())) != (major, minor):
            return EmptyMarker()
    if interpreter == "py":
        if tag.abi != "none":
            return EmptyMarker()
        implementation_environment: BaseMarker = AnyMarker()
    else:
        implementation_environment = _implementation_environment(interpreter)

    if tag.abi in {"abi3", "abi3t"} and (
        interpreter != "cp" or (major, minor) < (3, 2)
    ):
        return EmptyMarker()
    if tag.abi == "abi3t":
        # PEP 803 starts supported abi3t builds at 3.15. packaging deliberately
        # accepts older cpXY targets for experimental backports, which the
        # registry does not promise. Preserve any higher target's own floor.
        runtime_major, runtime_minor = max((major, minor), (3, 15))
        version = f"{runtime_major}.{runtime_minor}"
    if interpreter == "py" or tag.abi in {"abi3", "abi3t"}:
        version_environment = MultiMarker.of(
            from_pkg_marker(Marker(f'python_version >= "{version}"')),
            from_pkg_marker(Marker(f'python_version < "{major + 1}"')),
        )
    else:
        version_environment = from_pkg_marker(
            Marker(f'python_version == "{version}"')
        )
    return MultiMarker.of(implementation_environment, version_environment)


def _operating_system_environment(system: str) -> BaseMarker:
    values = {
        "linux": ("posix", "linux", "Linux"),
        "macos": ("posix", "darwin", "Darwin"),
        "windows": ("nt", "win32", "Windows"),
    }
    os_name, sys_platform, platform_system = values[system]
    return MultiMarker.of(
        _equals_marker("os_name", os_name),
        _equals_marker("sys_platform", sys_platform),
        _equals_marker("platform_system", platform_system),
    )


@lru_cache(maxsize=PACKAGE_WHEEL_TAGS_MAX_COUNT)
def _macos_architectures(platform_tag: str) -> frozenset[str]:
    match = _MACOS_PLATFORM_RE.fullmatch(platform_tag)
    if match is None:
        return frozenset()
    major, minor, wheel_architecture = match.groups()
    major_version = int(major)
    minor_version = int(minor)
    if major_version > 99 or minor_version > 99:
        return frozenset()
    target_version = (
        max(major_version, 11),
        0 if major_version >= 11 else minor_version,
    )
    architectures = frozenset(
        architecture
        for architecture in ("arm64", "x86_64")
        if platform_tag in set(mac_platforms(target_version, architecture))
    )
    if architectures:
        return architectures
    if wheel_architecture in {"arm64", "x86_64"}:
        return frozenset((wheel_architecture,))
    return frozenset()


def _platform_shape(platform_tag: str) -> tuple[str, frozenset[str]]:
    if platform_tag == "any":
        return "any", frozenset()
    linux_match = _LINUX_PLATFORM_RE.fullmatch(platform_tag)
    if linux_match is not None:
        policy, architecture = linux_match.groups()
        if policy.startswith("manylinux"):
            family = "linux-glibc"
        elif policy.startswith("musllinux"):
            family = "linux-musl"
        else:
            family = "linux-any"
        return family, frozenset((architecture,))
    if platform_tag == "win32":
        return "windows", frozenset(("x86",))
    if platform_tag.startswith("win_"):
        architecture = platform_tag.removeprefix("win_")
        machine = {"amd64": "AMD64", "arm64": "ARM64"}.get(
            architecture, architecture
        )
        return "windows", frozenset((machine,))
    macos_architectures = _macos_architectures(platform_tag)
    if macos_architectures:
        return "macos", macos_architectures
    return f"exact:{platform_tag}", frozenset()


def _wheel_platform_environment(platform_tag: str) -> BaseMarker:
    family, architectures = _platform_shape(platform_tag)
    if family == "any":
        return AnyMarker()
    if family.startswith("linux-"):
        operating_system = _operating_system_environment("linux")
    elif family == "windows":
        operating_system = _operating_system_environment("windows")
    elif family == "macos":
        operating_system = _operating_system_environment("macos")
    else:
        return EmptyMarker()
    machine_environment = (
        _one_of_marker("platform_machine", architectures)
        if architectures
        else AnyMarker()
    )
    return MultiMarker.of(operating_system, machine_environment)


def _platform_tags_overlap(left: str, right: str) -> bool:
    left_family, left_architectures = _platform_shape(left)
    right_family, right_architectures = _platform_shape(right)
    if "any" in {left_family, right_family}:
        return True
    if left_family.startswith("linux-") and right_family.startswith("linux-"):
        libc_compatible = (
            left_family == right_family
            or "linux-any" in {left_family, right_family}
        )
        return libc_compatible and bool(left_architectures & right_architectures)
    if left_family != right_family:
        return False
    if left_family in {"windows", "macos"}:
        return bool(left_architectures & right_architectures)
    return left_family == right_family


@lru_cache(maxsize=PACKAGE_WHEEL_TAGS_MAX_COUNT)
def _abi_tags_overlap(left: str, right: str) -> bool:
    if left == "none" or right == "none" or left == right:
        return True
    for runtime_abi, required_abi in ((left, right), (right, left)):
        match = _CPYTHON_ABI_RE.fullmatch(runtime_abi)
        if match is None:
            continue
        major, minor, _threaded, debug, _pymalloc, _ucs4 = match.groups()
        version = (int(major), int(minor))
        if version[1] > 99:
            continue
        abis = [runtime_abi]
        if debug and version >= (3, 8):
            abis.append(runtime_abi.removesuffix("d"))
        if any(
            tag.abi == required_abi
            for tag in cpython_tags(version, abis=abis, platforms=("any",))
        ):
            return True
    return False


def _wheel_tag_environment(tag: Tag) -> BaseMarker:
    return MultiMarker.of(
        _wheel_python_environment(tag),
        _wheel_platform_environment(tag.platform),
    )


def _wheel_tags_overlap(left: Tag, right: Tag) -> bool:
    return _abi_tags_overlap(left.abi, right.abi) and _platform_tags_overlap(
        left.platform, right.platform
    )


def _wheel_set_matches_environment(
    wheel_tags: frozenset[Tag], environment: BaseMarker
) -> bool:
    return any(
        not MultiMarker.of(environment, _wheel_tag_environment(tag)).is_empty()
        for tag in wheel_tags
    )


def _wheel_sets_overlap(
    provider_tags: frozenset[Tag],
    dependency_tags: frozenset[Tag],
    *,
    provider_python_environment: BaseMarker,
    dependency_python_environment: BaseMarker,
    condition: BaseMarker,
) -> bool:
    applicable = False
    for provider_tag in provider_tags:
        provider_environment = intersection(
            provider_python_environment,
            condition,
            _wheel_tag_environment(provider_tag),
        )
        if provider_environment.is_empty():
            continue
        applicable = True
        for dependency_tag in dependency_tags:
            if not _wheel_tags_overlap(provider_tag, dependency_tag):
                continue
            shared_environment = intersection(
                provider_environment,
                dependency_python_environment,
                _wheel_tag_environment(dependency_tag),
            )
            if not shared_environment.is_empty():
                return True
    return not applicable


def _validate_wheel_closure(
    distribution_name: str,
    selected_conditions: Mapping[tuple[str, str], BaseMarker],
    release_python_environments: Mapping[tuple[str, str], BaseMarker],
    release_wheel_tags: Mapping[tuple[str, str], frozenset[Tag]],
) -> None:
    """Find one environment that can install all simultaneously active releases."""
    releases = sorted(
        selected_conditions,
        key=lambda key: (
            not selected_conditions[key].is_any(),
            len(release_wheel_tags[key]),
            key,
        ),
    )
    candidates = {
        key: tuple(
            (
                tag,
                intersection(
                    release_python_environments[key], _wheel_tag_environment(tag)
                ),
            )
            for tag in sorted(release_wheel_tags[key], key=str)
        )
        for key in releases
    }
    remaining_steps = WHEEL_CLOSURE_MAX_STEPS

    def search(
        offset: int, environment: BaseMarker, selected_tags: tuple[Tag, ...]
    ) -> bool:
        nonlocal remaining_steps
        remaining_steps -= 1
        if remaining_steps < 0:
            _fail(f"wheel compatibility search is too complex for {distribution_name}")
        if environment.is_empty():
            return False
        if offset == len(releases):
            return True
        key = releases[offset]
        condition = selected_conditions[key]
        if not condition.is_any():
            inactive = intersection(environment, ~condition)
            if not inactive.is_empty() and search(offset + 1, inactive, selected_tags):
                return True
        active = intersection(environment, condition)
        if active.is_empty():
            return False
        for tag, wheel_environment in candidates[key]:
            remaining_steps -= 1
            if remaining_steps < 0:
                _fail(
                    f"wheel compatibility search is too complex for {distribution_name}"
                )
            if not all(
                _wheel_tags_overlap(tag, selected) for selected in selected_tags
            ):
                continue
            shared = intersection(active, wheel_environment)
            if not shared.is_empty() and search(
                offset + 1, shared, (*selected_tags, tag)
            ):
                return True
        return False

    if not search(0, AnyMarker(), ()):
        _fail(f"{distribution_name} internal wheel closure has no common environment")


def _conditioned_requirement(
    requirement: Requirement,
    inherited_condition: BaseMarker,
    python_environment: BaseMarker,
    distribution_name: str,
) -> tuple[Requirement, BaseMarker] | None:
    base_requirement = _requirement_for_base_install(requirement)
    if base_requirement is None:
        return None
    try:
        own_condition = (
            AnyMarker()
            if base_requirement.marker is None
            else from_pkg_marker(base_requirement.marker)
        )
        condition = MultiMarker.of(inherited_condition, own_condition)
        if MultiMarker.of(python_environment, condition).is_empty():
            return None
        conditioned = copy(base_requirement)
        conditioned.marker = None
        return conditioned, condition
    except (KeyError, TypeError, ValueError) as exception:
        raise SiteBuildError(
            f"package marker cannot be combined for {distribution_name}"
        ) from exception


def _requirement_with_condition(
    requirement: Requirement, condition: BaseMarker
) -> str:
    conditioned = copy(requirement)
    conditioned.marker = None if condition.is_any() else Marker(str(condition))
    return str(conditioned)


def _internal_pin_with_condition(
    distribution_name: str, version: str, condition: BaseMarker
) -> str:
    pin = f"{distribution_name}==={version}"
    return pin if condition.is_any() else f"{pin}; {condition}"


def _conditions_overlap(
    python_environment: BaseMarker,
    left: BaseMarker,
    right: BaseMarker,
) -> bool:
    return not MultiMarker.of(python_environment, left, right).is_empty()


def _parse_public_lock(
    contents: bytes, *, distribution_name: str, reject_vane: bool
) -> tuple[str, ...]:
    if len(contents) > PUBLIC_LOCK_MAX_BYTES:
        _fail(f"public dependency lock is too large for {distribution_name}")
    try:
        lines = contents.decode("utf-8").splitlines()
    except UnicodeError as exception:
        raise SiteBuildError(
            f"public dependency lock is invalid for {distribution_name}"
        ) from exception

    locked_requirements: set[str] = set()
    for line in lines:
        requirement_text = line.strip()
        if not requirement_text:
            continue
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exception:
            raise SiteBuildError(
                f"public dependency lock is invalid for {distribution_name}"
            ) from exception
        normalized_name = canonicalize_name(requirement.name)
        specifiers = list(requirement.specifier)
        if (
            requirement.url is not None
            or requirement.extras
            or len(specifiers) != 1
            or specifiers[0].operator not in {"==", "==="}
            or "*" in specifiers[0].version
        ):
            _fail(f"public dependency lock is invalid for {distribution_name}")
        try:
            locked_version = str(Version(specifiers[0].version))
        except InvalidVersion as exception:
            raise SiteBuildError(
                f"public dependency lock is invalid for {distribution_name}"
            ) from exception
        if reject_vane and _is_vane_distribution(normalized_name):
            _fail(
                f"public dependency closure for {distribution_name} contains "
                f"Vane-owned package {normalized_name}"
            )
        if _requirement_for_base_install(requirement) is not requirement:
            _fail(
                f"public dependency lock contains an unresolved extra marker "
                f"for {distribution_name}"
            )
        locked_requirement = f"{normalized_name}=={locked_version}"
        if requirement.marker is not None:
            locked_requirement = f"{locked_requirement}; {requirement.marker}"
        locked_requirements.add(locked_requirement)
        if len(locked_requirements) > PUBLIC_LOCK_MAX_COUNT:
            _fail(f"public dependency lock is too large for {distribution_name}")
    return tuple(sorted(locked_requirements, key=str.casefold))


def _resolver_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PIP_", "UV_"))
    }


def _resolver_python_lower_bound(requires_python: str) -> str:
    """Return a conservative uv lower bound, independent of the build host."""
    try:
        specifier = from_specifierset(SpecifierSet(requires_python))
    except (TypeError, ValueError) as exception:
        raise SiteBuildError(
            "invalid Requires-Python for public resolution"
        ) from exception
    if isinstance(specifier, UnionSpecifier):
        specifier = specifier.ranges[0]
    if not isinstance(specifier, RangeSpecifier) or specifier.min is None:
        _fail("public resolution requires a finite Python lower bound")
    lower = specifier.min
    if lower.epoch or lower.is_prerelease or lower.is_postrelease or lower.local:
        _fail("public resolution requires a stable Python lower bound")
    # uv accepts an inclusive major.minor.patch bound. Keep an exclusive endpoint
    # as a conservative bound; root markers preserve the actual exclusion.
    # Do not round patch bounds up and omit a supported environment.
    if len(lower.release) > 3:
        _fail("public resolution requires a major.minor.patch Python lower bound")
    return ".".join(map(str, (*lower.release, 0, 0)[:3]))


def _resolver_requirements(
    requirements: tuple[str, ...], requires_python: str
) -> list[str]:
    # pip compile does not use project.requires-python. Carry its complete
    # range on the roots so upper bounds and exclusions constrain the graph.
    # Keep the original operators: normalizing ~= through a full-version marker
    # and then serializing it could change the significant release precision.
    python_condition = " and ".join(
        f"python_full_version {specifier.operator} {json.dumps(specifier.version)}"
        for specifier in sorted(SpecifierSet(requires_python), key=str)
    )
    result = []
    for text in requirements:
        requirement = Requirement(text)
        requirement.marker = Marker(
            python_condition
            if requirement.marker is None
            else f"({requirement.marker}) and ({python_condition})"
        )
        result.append(str(requirement))
    return result


class UvPublicDependencyResolver:
    """Build one universal, exact public dependency closure with uv."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, tuple[str, ...]], bytes] = {}
        self._inflight: dict[
            tuple[str, tuple[str, ...]], Future[bytes]
        ] = {}
        self._lock = Lock()

    def resolve(
        self,
        requirements: tuple[str, ...],
        *,
        requires_python: str,
        distribution_name: str,
        reject_vane: bool,
    ) -> tuple[str, ...]:
        normalized_requirements = tuple(sorted(set(requirements), key=str.casefold))
        if not normalized_requirements:
            return ()
        cache_key = (requires_python, normalized_requirements)
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                contents = cached
            else:
                future = self._inflight.get(cache_key)
                owns_resolution = future is None
                if future is None:
                    future = Future()
                    self._inflight[cache_key] = future
        if cached is None:
            if owns_resolution:
                try:
                    contents = self._resolve_uncached(
                        normalized_requirements,
                        requires_python=requires_python,
                    )
                except BaseException as exception:
                    future.set_exception(exception)
                    with self._lock:
                        self._inflight.pop(cache_key, None)
                    if isinstance(exception, SiteBuildError):
                        raise SiteBuildError(
                            "could not resolve public dependency closure for "
                            f"{distribution_name}"
                        ) from exception
                    raise
                else:
                    future.set_result(contents)
                    with self._lock:
                        self._cache[cache_key] = contents
                        self._inflight.pop(cache_key, None)
            else:
                try:
                    contents = future.result()
                except SiteBuildError as exception:
                    raise SiteBuildError(
                        "could not resolve public dependency closure for "
                        f"{distribution_name}"
                    ) from exception
        return _parse_public_lock(
            contents,
            distribution_name=distribution_name,
            reject_vane=reject_vane,
        )

    def _resolve_uncached(
        self,
        requirements: tuple[str, ...],
        *,
        requires_python: str,
    ) -> bytes:
        python_lower_bound = _resolver_python_lower_bound(requires_python)
        try:
            with tempfile.TemporaryDirectory(prefix="vane-public-lock-") as temporary:
                temporary_root = Path(temporary)
                project_path = temporary_root / "pyproject.toml"
                output_path = temporary_root / "requirements.txt"
                project_path.write_text(
                    tomli_w.dumps(
                        {
                            "project": {
                                "name": (
                                    "vane-registry-resolution-"
                                    f"{secrets.token_hex(16)}"
                                ),
                                "version": "0",
                                "requires-python": requires_python,
                                "dependencies": _resolver_requirements(
                                    requirements, requires_python
                                ),
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "uv",
                        "--quiet",
                        "--no-progress",
                        "--no-cache",
                        "pip",
                        "compile",
                        str(project_path),
                        "--universal",
                        "--python-version",
                        python_lower_bound,
                        "--no-header",
                        "--no-annotate",
                        "--no-strip-markers",
                        "--default-index",
                        _PACKAGE_INDEXES["pypi"]["simple"],
                        "--no-config",
                        "--no-sources",
                        "--no-python-downloads",
                        "--keyring-provider",
                        "disabled",
                        "--only-binary=:all:",
                        "--output-file",
                        str(output_path),
                    ],
                    cwd=temporary_root,
                    env=_resolver_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=PUBLIC_LOCK_TIMEOUT_SECONDS,
                    check=False,
                )
                if result.returncode != 0:
                    _fail("could not resolve public dependency closure")
                with output_path.open("rb") as output_file:
                    contents = output_file.read(PUBLIC_LOCK_MAX_BYTES + 1)
        except SiteBuildError:
            raise
        except (OSError, subprocess.SubprocessError) as exception:
            raise SiteBuildError(
                "could not resolve public dependency closure"
            ) from exception
        return contents


_PUBLIC_DEPENDENCY_RESOLVER = UvPublicDependencyResolver()


def _exact_internal_requirement(
    requirement: Requirement, parent_distribution: str
) -> tuple[str, str]:
    normalized_name = canonicalize_name(requirement.name)
    specifiers = list(requirement.specifier)
    if (
        requirement.url is not None
        or requirement.extras
        or len(specifiers) != 1
        or specifiers[0].operator not in {"==", "==="}
        or "*" in specifiers[0].version
    ):
        _fail(
            f"{parent_distribution} must pin Vane dependency "
            f"{normalized_name} to one exact package-index version"
        )
    version_text = specifiers[0].version
    try:
        Version(version_text)
    except InvalidVersion as exception:
        raise SiteBuildError(
            f"{parent_distribution} has an invalid Vane dependency version"
        ) from exception
    return normalized_name, version_text


def _release_requirements(
    requirement: Requirement,
    *,
    parent_distribution: str,
    package_index: str,
    client: JsonMetadataClient,
) -> tuple[str, BaseMarker, tuple[Requirement, ...], frozenset[Tag]]:
    distribution_name, requested_version = _exact_internal_requirement(
        requirement, parent_distribution
    )
    index = _PACKAGE_INDEXES[package_index]
    url = index["release_api"].format(
        distribution=quote(distribution_name, safe="-"),
        version=quote(requested_version, safe=""),
    )
    value = client.get_json(url)
    document = _mapping(
        value, f"package release metadata for {distribution_name}"
    )
    info = _mapping(
        document.get("info"), f"package release info for {distribution_name}"
    )
    reported_name = _string(info.get("name"), "package release info.name")
    reported_version = _string(info.get("version"), "package release info.version")
    if not isinstance(reported_version, str):
        _fail(f"package release version is missing for {distribution_name}")
    try:
        release_version = Version(reported_version)
    except InvalidVersion as exception:
        raise SiteBuildError(
            f"package release version is invalid for {distribution_name}"
        ) from exception
    if (
        not isinstance(reported_name, str)
        or canonicalize_name(reported_name) != distribution_name
        or not requirement.specifier.contains(reported_version, prereleases=True)
    ):
        _fail(
            f"package release metadata identity does not match {requirement}"
        )
    file_metadata, wheel_tags = _package_file_metadata(
        document, distribution_name, reported_version, release_version
    )
    if file_metadata["wheel_count"] == 0:
        _fail(f"{distribution_name} does not publish a non-yanked wheel")
    requires_python = _string(
        info.get("requires_python"),
        "package release info.requires_python",
        nullable=True,
    )
    python_environment = _python_environment_marker(
        requires_python, distribution_name
    )
    if not _wheel_set_matches_environment(
        wheel_tags, python_environment
    ):
        _fail(
            f"{distribution_name} has no non-yanked wheel compatible with "
            "its Requires-Python"
        )
    return (
        reported_version,
        python_environment,
        _package_requirements(info, distribution_name),
        wheel_tags,
    )


def _pip_install_arguments(
    index_url: str, requirements: tuple[str, ...]
) -> list[str]:
    return [
        *_PIP_COMMAND,
        "install",
        "--force-reinstall",
        "--no-deps",
        "--only-binary=:all:",
        "--index-url",
        index_url,
        *requirements,
    ]


def _posix_install_script(commands: list[list[str]]) -> str | None:
    if not commands:
        return None
    return " &&\n".join(
        shlex.join(["env", "PIP_CONFIG_FILE=/dev/null", *arguments])
        for arguments in commands
    )


def _powershell_quote(argument: str) -> str:
    """Return one literal PowerShell argument without interpolation."""
    escaped = argument.replace("'", "''")
    return f"'{escaped}'"


def _powershell_install_script(commands: list[list[str]]) -> str | None:
    if not commands:
        return None
    lines = [
        "& {",
        "  $__vanePipConfigFileWasSet = Test-Path Env:PIP_CONFIG_FILE",
        "  $__vanePipConfigFile = $env:PIP_CONFIG_FILE",
        "  try {",
        "    $env:PIP_CONFIG_FILE = 'nul'",
    ]
    for arguments in commands:
        quoted_arguments = " ".join(
            _powershell_quote(argument) for argument in arguments
        )
        lines.extend(
            [
                f"    & {quoted_arguments}",
                "    $__vanePipSucceeded = $?",
                "    $__vanePipExitCode = $LASTEXITCODE",
                (
                    "    if (-not $__vanePipSucceeded -or "
                    "$__vanePipExitCode -ne 0) {"
                ),
                '      throw "pip exited with code $__vanePipExitCode"',
                "    }",
            ]
        )
    lines.extend(
        [
            "  } finally {",
            "    if ($__vanePipConfigFileWasSet) {",
            "      $env:PIP_CONFIG_FILE = $__vanePipConfigFile",
            "    } else {",
            "      Remove-Item Env:PIP_CONFIG_FILE -ErrorAction SilentlyContinue",
            "    }",
            "  }",
            "}",
        ]
    )
    return "\n".join(lines)


def _testpypi_install_arguments(
    distribution_name: str,
    version_text: str,
    requires_python: object,
    requirements: tuple[Requirement, ...],
    provider_wheel_tags: frozenset[Tag],
    client: JsonMetadataClient,
    public_resolver: PublicDependencyResolver,
) -> list[list[str]]:
    root_name = canonicalize_name(distribution_name)
    python_environment = _python_environment_marker(
        requires_python, distribution_name
    )
    if not _wheel_set_matches_environment(
        provider_wheel_tags, python_environment
    ):
        _fail(
            f"{distribution_name} has no non-yanked wheel compatible with "
            "its Requires-Python"
        )
    root_key = (root_name, version_text)
    selected_conditions: dict[tuple[str, str], BaseMarker] = {
        root_key: AnyMarker()
    }
    release_requirements: dict[
        tuple[str, str], tuple[Requirement, ...]
    ] = {root_key: requirements}
    release_python_environments: dict[tuple[str, str], BaseMarker] = {
        root_key: python_environment
    }
    release_wheel_tags: dict[tuple[str, str], frozenset[Tag]] = {
        root_key: provider_wheel_tags
    }
    release_aliases: dict[tuple[str, str], tuple[str, str]] = {}
    pending = [(requirement, AnyMarker()) for requirement in requirements]
    public_requirements: set[str] = set()
    while pending:
        raw_requirement, inherited_condition = pending.pop()
        conditioned = _conditioned_requirement(
            raw_requirement,
            inherited_condition,
            python_environment,
            distribution_name,
        )
        if conditioned is None:
            continue
        requirement, condition = conditioned
        normalized_name = canonicalize_name(requirement.name)
        if not _is_vane_distribution(normalized_name):
            if requirement.url is not None:
                _fail(
                    f"{distribution_name} has an unsupported direct URL dependency"
                )
            public_requirements.add(
                _requirement_with_condition(requirement, condition)
            )
            if len(public_requirements) > PACKAGE_REQUIREMENTS_MAX_COUNT:
                _fail(f"public dependency closure is too large for {distribution_name}")
            continue
        dependency_name, requested_version = _exact_internal_requirement(
            requirement, distribution_name
        )
        selected_key = next(
            (
                key
                for key in selected_conditions
                if key[0] == dependency_name
                and requirement.specifier.contains(key[1], prereleases=True)
            ),
            None,
        )
        if selected_key is None:
            alias = (dependency_name, requested_version)
            selected_key = release_aliases.get(alias)
            if selected_key is None:
                (
                    reported_version,
                    child_python_environment,
                    child_requirements,
                    child_wheel_tags,
                ) = _release_requirements(
                    requirement,
                    parent_distribution=distribution_name,
                    package_index="testpypi",
                    client=client,
                )
                selected_key = (dependency_name, reported_version)
                release_aliases[alias] = selected_key
                release_requirements.setdefault(
                    selected_key, child_requirements
                )
                release_python_environments.setdefault(
                    selected_key, child_python_environment
                )
                release_wheel_tags.setdefault(selected_key, child_wheel_tags)

        dependency_python_environment = release_python_environments[selected_key]
        unsupported_environment = MultiMarker.of(
            python_environment,
            condition,
            ~dependency_python_environment,
        )
        if not unsupported_environment.is_empty():
            _fail(
                f"{dependency_name}=={selected_key[1]} Requires-Python is "
                f"incompatible with {distribution_name}"
            )
        if not _wheel_sets_overlap(
            provider_wheel_tags,
            release_wheel_tags[selected_key],
            provider_python_environment=python_environment,
            dependency_python_environment=dependency_python_environment,
            condition=condition,
        ):
            _fail(
                f"{dependency_name}=={selected_key[1]} has no non-yanked wheel "
                f"compatible with {distribution_name}"
            )
        for other_key, other_condition in selected_conditions.items():
            if (
                other_key[0] == dependency_name
                and other_key != selected_key
                and _conditions_overlap(
                    python_environment, condition, other_condition
                )
            ):
                _fail(f"Vane dependency versions conflict for {dependency_name}")

        previous_condition = selected_conditions.get(selected_key)
        merged_condition = (
            condition
            if previous_condition is None
            else MarkerUnion.of(previous_condition, condition)
        )
        if previous_condition is not None and merged_condition == previous_condition:
            continue
        if (
            previous_condition is None
            and len(selected_conditions) >= VANE_REQUIREMENTS_MAX_COUNT
        ):
            _fail(f"Vane dependency closure is too large for {distribution_name}")
        selected_conditions[selected_key] = merged_condition
        pending.extend(
            (child_requirement, condition)
            for child_requirement in release_requirements[selected_key]
        )

    _validate_wheel_closure(
        distribution_name,
        selected_conditions,
        release_python_environments,
        release_wheel_tags,
    )
    commands: list[list[str]] = []
    if public_requirements:
        if not isinstance(requires_python, str):
            _fail(
                f"{distribution_name} must declare Requires-Python to resolve "
                "public dependencies"
            )
        public_lock = public_resolver.resolve(
            tuple(sorted(public_requirements, key=str.casefold)),
            requires_python=requires_python,
            distribution_name=distribution_name,
            reject_vane=True,
        )
        if public_lock:
            commands.append(
                _pip_install_arguments(
                    _PACKAGE_INDEXES["pypi"]["simple"], public_lock
                )
            )
    commands.append(
        _pip_install_arguments(
            _PACKAGE_INDEXES["testpypi"]["simple"],
            tuple(
                _internal_pin_with_condition(name, version, condition)
                for (name, version), condition in sorted(
                    selected_conditions.items()
                )
            ),
        )
    )
    return commands


def _pypi_install_arguments(
    distribution_name: str,
    version_text: str,
    package: Mapping[str, object],
    public_resolver: PublicDependencyResolver,
) -> list[str]:
    if package["wheel_count"] == 0:
        _fail(f"{distribution_name} does not publish a non-yanked wheel")
    requires_python = package["requires_python"]
    if not isinstance(requires_python, str):
        _fail(
            f"{distribution_name} must declare Requires-Python to resolve "
            "its PyPI dependency closure"
        )
    locked_requirements = public_resolver.resolve(
        (f"{distribution_name}==={version_text}",),
        requires_python=requires_python,
        distribution_name=distribution_name,
        reject_vane=False,
    )
    root_name = canonicalize_name(distribution_name)
    root_requirements = tuple(
        requirement
        for requirement in map(Requirement, locked_requirements)
        if canonicalize_name(requirement.name) == root_name
    )
    if (
        len(root_requirements) != 1
        or not root_requirements[0].specifier.contains(
            version_text, prereleases=True
        )
    ):
        _fail(f"PyPI dependency lock omits {distribution_name}")
    root = root_requirements[0]
    if root.marker is not None:
        supported = _python_environment_marker(requires_python, distribution_name)
        if not intersection(supported, ~from_pkg_marker(root.marker)).is_empty():
            _fail(
                "PyPI dependency lock omits supported environments for "
                f"{distribution_name}"
            )
        # Keep the provider itself unconditional so pip reports an unsupported
        # interpreter instead of silently skipping the entire installation.
        root.marker = None
        locked_requirements = tuple(
            str(root)
            if canonicalize_name(Requirement(text).name) == root_name
            else text
            for text in locked_requirements
        )
    return _pip_install_arguments(
        _PACKAGE_INDEXES["pypi"]["simple"], locked_requirements
    )


def _installation_metadata(
    extension_name: str,
    distribution_name: str,
    package_index: str,
    package: Mapping[str, object],
    requirements: tuple[Requirement, ...],
    provider_wheel_tags: frozenset[Tag],
    client: JsonMetadataClient,
    public_resolver: PublicDependencyResolver,
) -> dict[str, object]:
    version_text = package["latest_version"]
    if not package["published"]:
        install_arguments: list[list[str]] = []
    elif not isinstance(version_text, str):
        _fail(f"published package version is missing for {distribution_name}")
    elif package_index == "testpypi":
        if package["wheel_count"] == 0:
            _fail(f"{distribution_name} does not publish a non-yanked wheel")
        install_arguments = _testpypi_install_arguments(
            distribution_name,
            version_text,
            package["requires_python"],
            requirements,
            provider_wheel_tags,
            client,
            public_resolver,
        )
    else:
        install_arguments = [
            _pypi_install_arguments(
                distribution_name,
                version_text,
                package,
                public_resolver,
            )
        ]
    posix_install_script = _posix_install_script(install_arguments)
    powershell_install_script = _powershell_install_script(install_arguments)
    if any(
        len(script) > INSTALL_SCRIPT_MAX_LENGTH
        for script in (posix_install_script, powershell_install_script)
        if script is not None
    ):
        _fail(f"generated install script is too long for {distribution_name}")
    load_example = (
        "import vane\n\n"
        'connection = vane.connect(\":memory:\")\n'
        f'vane.load_installed_extension("{extension_name}", connection=connection)'
    )
    return {
        "posix_install_script": posix_install_script,
        "powershell_install_script": powershell_install_script,
        "load_example": load_example,
    }


def _detail_record(
    manifest: Mapping[str, object],
    *,
    generated_at: str,
    client: JsonMetadataClient,
    github_token: str | None,
    public_resolver: PublicDependencyResolver,
) -> dict[str, object]:
    extension_name = str(manifest["extension_name"])
    distribution_name = str(manifest["distribution_name"])
    repository = str(manifest["repository"])
    package_index = str(manifest["package_index"])
    package, requirements, provider_wheel_tags = _package_metadata(
        distribution_name, package_index, client
    )
    metrics = _download_metadata(
        distribution_name, package_index, bool(package["published"]), client
    )
    return {
        "$schema": "../../schema/extension-detail.schema.json",
        "format_version": DETAIL_FORMAT_VERSION,
        "generated_at": generated_at,
        "extension_name": extension_name,
        "distribution_name": distribution_name,
        "description": manifest["description"],
        "repository": repository,
        "publisher": manifest["publisher"],
        "license": manifest["license"],
        "maintainers": manifest["maintainers"],
        "documentation": manifest["documentation"],
        "source": _github_metadata(repository, client, github_token),
        "package": package,
        "installation": _installation_metadata(
            extension_name,
            distribution_name,
            package_index,
            package,
            requirements,
            provider_wheel_tags,
            client,
            public_resolver,
        ),
        "metrics": metrics,
    }


def build_details(
    *,
    manifest_root: Path = DEFAULT_MANIFEST_ROOT,
    generated_at: str,
    client: JsonMetadataClient,
    github_token: str | None = None,
    public_resolver: PublicDependencyResolver = _PUBLIC_DEPENDENCY_RESOLVER,
) -> tuple[dict[str, object], ...]:
    """Return enriched detail records for every reviewed manifest."""
    _timestamp(generated_at, "generated_at")
    manifests = load_manifests(manifest_root)
    if not manifests:
        _fail("registry must contain at least one extension manifest")
    with ThreadPoolExecutor(
        max_workers=min(METADATA_MAX_WORKERS, len(manifests)),
        thread_name_prefix="registry-metadata",
    ) as executor:
        return tuple(
            executor.map(
                lambda manifest: _detail_record(
                    manifest,
                    generated_at=generated_at,
                    client=client,
                    github_token=github_token,
                    public_resolver=public_resolver,
                ),
                manifests,
            )
        )


def _json_bytes(value: object, *, max_bytes: int = DETAIL_MAX_JSON_BYTES) -> bytes:
    contents = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()
    if len(contents) > max_bytes:
        _fail("generated site JSON exceeds its size limit")
    return contents


def _write_json(
    path: Path, value: object, *, max_bytes: int = DETAIL_MAX_JSON_BYTES
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value, max_bytes=max_bytes))


def _summary_record(detail: Mapping[str, object]) -> dict[str, object]:
    package = _mapping(detail["package"], "package")
    installation = _mapping(detail["installation"], "installation")
    return {
        "extension_name": detail["extension_name"],
        "distribution_name": detail["distribution_name"],
        "description": detail["description"],
        "repository": detail["repository"],
        "publisher": detail["publisher"],
        "license": detail["license"],
        "source": detail["source"],
        "package": {
            key: package[key]
            for key in (
                "index",
                "published",
                "latest_version",
                "wheel_count",
                "python_versions",
                "python_tags",
            )
        },
        "installation": {
            "posix_install_script": installation["posix_install_script"],
            "powershell_install_script": installation["powershell_install_script"],
        },
    }


def _detail_html(detail: Mapping[str, object]) -> str:
    documentation = _mapping(detail["documentation"], "documentation")
    source = _mapping(detail["source"], "source")
    package = _mapping(detail["package"], "package")
    installation = _mapping(detail["installation"], "installation")
    metrics = _mapping(detail["metrics"], "metrics")
    maintainers = detail["maintainers"]
    if not isinstance(maintainers, list):
        _fail("maintainers must be a list")
    latest_version = (
        str(package["latest_version"])
        if package["published"]
        else "Not published"
    )
    python_versions = ", ".join(str(value) for value in package["python_versions"])
    if not python_versions:
        python_versions = (
            ", ".join(str(value) for value in package["python_tags"])
            if package["wheel_count"]
            else "No wheels published"
        )
    platform_tags = (
        ", ".join(str(value) for value in package["platform_tags"]) or "None"
    )
    abi_tags = ", ".join(str(value) for value in package["abi_tags"]) or "None"
    downloads = (
        str(metrics["downloads_last_week"])
        if metrics["downloads_last_week"] is not None
        else f"Unavailable ({package['index']})"
    )
    return _TEMPLATE_ENVIRONMENT.get_template("extension.html.j2").render(
        detail=detail,
        documentation=documentation,
        source=source,
        package=package,
        installation=installation,
        maintainers=[
            {
                "name": str(maintainer),
                "url": f"https://github.com/{quote(str(maintainer), safe='')}",
            }
            for maintainer in maintainers
        ],
        latest_version=latest_version,
        python_versions=python_versions,
        abi_tags=abi_tags,
        platform_tags=platform_tags,
        downloads=downloads,
        machine_url=(
            "../../v1/extensions/"
            f"{quote(str(detail['extension_name']), safe='')}.json"
        ),
    )


def assemble_site(
    output: Path,
    *,
    generated_at: str,
    client: JsonMetadataClient,
    github_token: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Create a complete Pages tree without modifying the stable source catalog."""
    if output.exists():
        _fail(f"site output already exists: {output}")
    checked_in_catalog = PROJECT_ROOT / "index.json"
    if checked_in_catalog.read_bytes() != catalog_bytes():
        _fail("index.json is stale; regenerate it before building the site")
    details = build_details(
        generated_at=generated_at,
        client=client,
        github_token=github_token,
    )

    (output / "v1" / "extensions").mkdir(parents=True)
    shutil.copyfile(PROJECT_ROOT / "site" / "index.html", output / "index.html")
    shutil.copyfile(checked_in_catalog, output / "v1" / "index.json")
    (output / "schema").mkdir()
    for schema_path in sorted((PROJECT_ROOT / "schema").glob("*.json")):
        shutil.copyfile(schema_path, output / "schema" / schema_path.name)
    (output / ".nojekyll").touch()

    _write_json(
        output / "v1" / "extensions" / "index.json",
        {
            "$schema": "../../schema/extension-details-index.schema.json",
            "format_version": DETAIL_FORMAT_VERSION,
            "generated_at": generated_at,
            "extensions": [_summary_record(detail) for detail in details],
        },
        max_bytes=AGGREGATE_MAX_JSON_BYTES,
    )
    _write_json(
        output / "v1" / "metrics" / "downloads-last-week.json",
        {
            "$schema": "../../schema/download-metrics.schema.json",
            "format_version": DETAIL_FORMAT_VERSION,
            "generated_at": generated_at,
            "period": "last_week",
            "extensions": [
                {
                    "extension_name": detail["extension_name"],
                    "distribution_name": detail["distribution_name"],
                    "package_index": _mapping(detail["package"], "package")[
                        "index"
                    ],
                    **dict(_mapping(detail["metrics"], "metrics")),
                }
                for detail in details
            ],
        },
    )
    for detail in details:
        extension_name = str(detail["extension_name"])
        _write_json(
            output / "v1" / "extensions" / f"{extension_name}.json", detail
        )
        detail_directory = output / "extensions" / extension_name
        detail_directory.mkdir(parents=True)
        (detail_directory / "index.html").write_text(
            _detail_html(detail), encoding="utf-8"
        )
    return details


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "_site")
    parser.add_argument("--generated-at")
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    generated_at = arguments.generated_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    try:
        with MetadataClient() as client:
            details = assemble_site(
                arguments.output,
                generated_at=generated_at,
                client=client,
                github_token=os.environ.get("GITHUB_TOKEN"),
            )
    except (SiteBuildError, OSError) as exception:
        print(f"site generation failed: {exception}", file=sys.stderr)
        return 1
    print(f"Generated {arguments.output} with {len(details)} extensions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
