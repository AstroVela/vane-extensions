"""Build the enriched static registry site from reviewed manifests and live metadata."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, Protocol
from urllib.parse import quote, urlsplit

import httpx
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from packaging.specifiers import InvalidSpecifier, SpecifierSet
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
_PYTHON_TAG_RE = re.compile(r"^(?:cp|pp)([0-9])([0-9]+)$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)
_PACKAGE_INDEXES = {
    "pypi": {
        "api": "https://pypi.org/pypi/{distribution}/json",
        "project": "https://pypi.org/project/{distribution}/",
    },
    "testpypi": {
        "api": "https://test.pypi.org/pypi/{distribution}/json",
        "project": "https://test.pypi.org/project/{distribution}/",
    },
}
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
    return f"{match.group(1)}.{int(match.group(2))}"


def _package_metadata(
    distribution_name: str, package_index: str, client: JsonMetadataClient
) -> dict[str, object]:
    index = _PACKAGE_INDEXES[package_index]
    escaped_distribution = quote(distribution_name, safe="-")
    api_url = index["api"].format(distribution=escaped_distribution)
    project_url = index["project"].format(distribution=escaped_distribution)
    value = client.get_json(api_url, allow_not_found=True)
    if value is None:
        return {
            "index": package_index,
            "project_url": project_url,
            "published": False,
            "latest_version": None,
            "requires_python": None,
            "wheel_count": 0,
            "python_versions": [],
            "python_tags": [],
            "abi_tags": [],
            "platform_tags": [],
            "latest_release_uploaded_at": None,
        }

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
    raw_files = document.get("urls")
    if not isinstance(raw_files, list):
        _fail(f"package urls must be a list for {distribution_name}")
    wheel_count = 0
    python_versions: set[str] = set()
    python_tags: set[str] = set()
    abi_tags: set[str] = set()
    platform_tags: set[str] = set()
    upload_times: list[tuple[datetime, str]] = []
    for raw_file in raw_files:
        package_file = _mapping(raw_file, f"package file for {distribution_name}")
        package_type = _string(package_file.get("packagetype"), "package packagetype")
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
            wheel_name, wheel_version, _build, wheel_tags = parse_wheel_filename(
                filename
            )
        except InvalidWheelFilename as exception:
            raise SiteBuildError(
                f"package returned an invalid wheel filename for {distribution_name}"
            ) from exception
        if (
            canonicalize_name(wheel_name) != canonicalize_name(distribution_name)
            or wheel_version != latest_version
        ):
            _fail(f"wheel identity does not match {distribution_name}=={version_text}")
        wheel_count += 1
        for tag in wheel_tags:
            python_tags.add(tag.interpreter)
            abi_tags.add(tag.abi)
            platform_tags.add(tag.platform)
            python_version = _python_version_from_tag(tag.interpreter)
            if python_version is not None:
                python_versions.add(python_version)

    latest_release_uploaded_at = (
        max(upload_times, key=lambda item: item[0])[1] if upload_times else None
    )
    return {
        "index": package_index,
        "project_url": project_url,
        "published": True,
        "latest_version": version_text,
        "requires_python": requires_python,
        "wheel_count": wheel_count,
        "python_versions": sorted(python_versions, key=Version),
        "python_tags": sorted(python_tags),
        "abi_tags": sorted(abi_tags),
        "platform_tags": sorted(platform_tags),
        "latest_release_uploaded_at": latest_release_uploaded_at,
    }


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


def _installation_metadata(
    extension_name: str, distribution_name: str, package_index: str
) -> dict[str, str]:
    if package_index == "testpypi":
        install_command = (
            "python -m pip install --pre "
            "--index-url https://test.pypi.org/simple/ "
            "--extra-index-url https://pypi.org/simple/ "
            f"vane-ai {distribution_name}"
        )
    else:
        install_command = f"python -m pip install vane-ai {distribution_name}"
    load_example = (
        "import vane\n\n"
        'connection = vane.connect(\":memory:\")\n'
        f'vane.load_installed_extension("{extension_name}", connection=connection)'
    )
    return {"install_command": install_command, "load_example": load_example}


def _detail_record(
    manifest: Mapping[str, object],
    *,
    generated_at: str,
    client: JsonMetadataClient,
    github_token: str | None,
) -> dict[str, object]:
    extension_name = str(manifest["extension_name"])
    distribution_name = str(manifest["distribution_name"])
    repository = str(manifest["repository"])
    package_index = str(manifest["package_index"])
    package = _package_metadata(distribution_name, package_index, client)
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
            extension_name, distribution_name, package_index
        ),
        "metrics": metrics,
    }


def build_details(
    *,
    manifest_root: Path = DEFAULT_MANIFEST_ROOT,
    generated_at: str,
    client: JsonMetadataClient,
    github_token: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Return enriched detail records for every reviewed manifest."""
    _timestamp(generated_at, "generated_at")
    manifests = load_manifests(manifest_root)
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
        "installation": {"install_command": installation["install_command"]},
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
