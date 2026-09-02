"""Validate extension manifests and build the deterministic public catalog."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

CATALOG_FORMAT_VERSION = 1
CATALOG_MAX_BYTES = 64 * 1024
MANIFEST_FILENAME = "extension.json"
MANIFEST_MAX_BYTES = 64 * 1024
MANIFEST_MAX_COUNT = 1024
SCHEMA_REFERENCE = "../../schema/extension.schema.json"
EXTENSION_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
DISTRIBUTION_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
MANIFEST_KEYS = frozenset(
    {
        "$schema",
        "extension_name",
        "distribution_name",
        "description",
        "repository",
        "publisher",
        "license",
        "maintainers",
        "package_index",
        "documentation",
    }
)
DOCUMENTATION_KEYS = frozenset({"url", "hello_world", "extended_description"})
CATALOG_KEYS = (
    "extension_name",
    "distribution_name",
    "description",
    "repository",
    "publisher",
    "license",
)
PACKAGE_INDEXES = frozenset({"pypi", "testpypi"})
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_ROOT = PROJECT_ROOT / "extensions"


class CatalogBuildError(ValueError):
    """A registry manifest or generated catalog is invalid."""


def _fail(message: str) -> NoReturn:
    raise CatalogBuildError(message)


def _reject_duplicate_object_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"JSON object repeats key {key!r}")
        result[key] = value
    return result


def _read_bounded_regular_file(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        _fail(f"{path} must be a regular file")
    try:
        size = path.stat().st_size
    except OSError as exception:
        raise CatalogBuildError(f"could not inspect {path}") from exception
    if size > max_bytes:
        _fail(f"{path} exceeds the {max_bytes}-byte limit")
    try:
        contents = path.read_bytes()
    except OSError as exception:
        raise CatalogBuildError(f"could not read {path}") from exception
    if len(contents) > max_bytes:
        _fail(f"{path} exceeds the {max_bytes}-byte limit")
    return contents


def _manifest_string(value: object, field: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"{field} must be a non-empty trimmed string")
    if len(value) > max_length:
        _fail(f"{field} exceeds its {max_length}-character limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _fail(f"{field} must not contain control characters")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _fail(f"{field} must not contain lone Unicode surrogates")
    return value


def _manifest_multiline_string(
    value: object, field: str, *, max_length: int
) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"{field} must be a non-empty trimmed string")
    if len(value) > max_length:
        _fail(f"{field} exceeds its {max_length}-character limit")
    if any(
        (ord(character) < 32 and character != "\n") or ord(character) == 127
        for character in value
    ):
        _fail(f"{field} must not contain control characters other than newlines")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _fail(f"{field} must not contain lone Unicode surrogates")
    return value


def _validate_https_url(value: object, field: str) -> str:
    url = _manifest_string(value, field, max_length=2048)
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exception:
        raise CatalogBuildError(f"{field} is not a valid URL") from exception
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not url.isascii()
        or any(character.isspace() for character in url)
    ):
        _fail(f"{field} must be a canonical HTTPS URL")
    return url


def _validate_github_repository(value: object) -> str:
    repository = _validate_https_url(value, "repository")
    parsed = urlsplit(repository)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.netloc != "github.com"
        or len(parts) != 2
        or not all(parts)
        or not GITHUB_LOGIN_RE.fullmatch(parts[0])
        or not GITHUB_REPOSITORY_RE.fullmatch(parts[1])
        or repository != f"https://github.com/{parts[0]}/{parts[1]}"
        or parts[1].endswith(".git")
    ):
        _fail("repository must identify one canonical GitHub repository")
    return repository


def _load_manifest(path: Path, directory_name: str) -> dict[str, object]:
    contents = _read_bounded_regular_file(path, max_bytes=MANIFEST_MAX_BYTES)
    try:
        value = json.loads(
            contents.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_pairs,
        )
    except CatalogBuildError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exception:
        raise CatalogBuildError(f"{path} is not valid UTF-8 JSON") from exception
    if not isinstance(value, dict) or set(value) != MANIFEST_KEYS:
        _fail(f"{path} must contain exactly the registry manifest fields")
    if value["$schema"] != SCHEMA_REFERENCE:
        _fail(f"{path} must use $schema {SCHEMA_REFERENCE!r}")

    extension_name = _manifest_string(
        value["extension_name"], "extension_name", max_length=128
    )
    if not EXTENSION_NAME_RE.fullmatch(extension_name):
        _fail("extension_name must use lowercase ASCII extension-name syntax")
    if extension_name != directory_name:
        _fail(
            f"extension_name {extension_name!r} must match directory {directory_name!r}"
        )

    distribution_name = _manifest_string(
        value["distribution_name"], "distribution_name", max_length=256
    )
    if not DISTRIBUTION_NAME_RE.fullmatch(distribution_name):
        _fail(
            "distribution_name must be a canonical lowercase Python distribution name"
        )
    expected_distribution = f"vane-extension-{extension_name.replace('_', '-')}"
    if distribution_name != expected_distribution:
        _fail(f"distribution_name must be {expected_distribution!r}")

    maintainers_value = value["maintainers"]
    if (
        not isinstance(maintainers_value, list)
        or not maintainers_value
        or len(maintainers_value) > 20
    ):
        _fail("maintainers must be a non-empty list with at most 20 entries")
    maintainers: list[str] = []
    for maintainer_value in maintainers_value:
        maintainer = _manifest_string(
            maintainer_value, "maintainers entry", max_length=39
        )
        if not GITHUB_LOGIN_RE.fullmatch(maintainer):
            _fail("maintainers entries must be GitHub logins")
        maintainers.append(maintainer)
    if len(maintainers) != len({maintainer.casefold() for maintainer in maintainers}):
        _fail("maintainers must not contain duplicates")

    package_index = _manifest_string(
        value["package_index"], "package_index", max_length=16
    )
    if package_index not in PACKAGE_INDEXES:
        _fail("package_index must be either 'pypi' or 'testpypi'")

    documentation_value = value["documentation"]
    if (
        not isinstance(documentation_value, dict)
        or set(documentation_value) != DOCUMENTATION_KEYS
    ):
        _fail("documentation must contain exactly url, hello_world, and extended_description")

    return {
        "extension_name": extension_name,
        "distribution_name": distribution_name,
        "description": _manifest_string(
            value["description"], "description", max_length=500
        ),
        "repository": _validate_github_repository(value["repository"]),
        "publisher": _manifest_string(value["publisher"], "publisher", max_length=100),
        "license": _manifest_string(value["license"], "license", max_length=200),
        "maintainers": maintainers,
        "package_index": package_index,
        "documentation": {
            "url": _validate_https_url(documentation_value["url"], "documentation.url"),
            "hello_world": _manifest_multiline_string(
                documentation_value["hello_world"],
                "documentation.hello_world",
                max_length=4000,
            ),
            "extended_description": _manifest_multiline_string(
                documentation_value["extended_description"],
                "documentation.extended_description",
                max_length=12000,
            ),
        },
    }


def _manifest_paths(manifest_root: Path) -> Iterable[tuple[str, Path]]:
    if manifest_root.is_symlink() or not manifest_root.is_dir():
        _fail(f"{manifest_root} must be a directory")
    try:
        entries = sorted(manifest_root.iterdir(), key=lambda path: path.name)
    except OSError as exception:
        raise CatalogBuildError(f"could not enumerate {manifest_root}") from exception
    if not entries:
        _fail("registry must contain at least one extension manifest")
    if len(entries) > MANIFEST_MAX_COUNT:
        _fail(f"registry exceeds the {MANIFEST_MAX_COUNT}-manifest limit")
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            _fail(
                f"unexpected registry path {entry}; only extension directories are allowed"
            )
        if not EXTENSION_NAME_RE.fullmatch(entry.name):
            _fail(
                f"extension directory {entry.name!r} does not use extension-name syntax"
            )
        try:
            children = list(entry.iterdir())
        except OSError as exception:
            raise CatalogBuildError(f"could not enumerate {entry}") from exception
        if len(children) != 1 or children[0].name != MANIFEST_FILENAME:
            _fail(f"{entry} must contain only {MANIFEST_FILENAME}")
        yield entry.name, entry / MANIFEST_FILENAME


def load_manifests(
    manifest_root: Path = DEFAULT_MANIFEST_ROOT,
) -> tuple[dict[str, object], ...]:
    """Return all validated source manifests in deterministic order."""
    manifests = [
        _load_manifest(path, name) for name, path in _manifest_paths(manifest_root)
    ]
    names = [entry["extension_name"] for entry in manifests]
    if len(names) != len(set(names)):
        _fail("registry repeats an extension_name")
    distributions = [entry["distribution_name"] for entry in manifests]
    if len(distributions) != len(set(distributions)):
        _fail("registry repeats a distribution_name")
    manifests.sort(key=lambda entry: entry["extension_name"])
    return tuple(manifests)


def build_catalog(manifest_root: Path = DEFAULT_MANIFEST_ROOT) -> dict[str, object]:
    """Return the stable discovery catalog derived from *manifest_root*."""
    extensions = [
        {key: manifest[key] for key in CATALOG_KEYS}
        for manifest in load_manifests(manifest_root)
    ]
    return {"format_version": CATALOG_FORMAT_VERSION, "extensions": extensions}


def catalog_bytes(manifest_root: Path = DEFAULT_MANIFEST_ROOT) -> bytes:
    """Serialize the validated catalog using the repository's canonical form."""
    contents = (
        json.dumps(build_catalog(manifest_root), indent=2, ensure_ascii=False) + "\n"
    ).encode()
    if len(contents) > CATALOG_MAX_BYTES:
        _fail(f"generated catalog exceeds the {CATALOG_MAX_BYTES}-byte limit")
    return contents


def _write_atomic(path: Path, contents: bytes) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(path)
    except OSError as exception:
        raise CatalogBuildError(
            f"could not write generated catalog to {path}"
        ) from exception
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--output", type=Path, help="write the generated catalog atomically"
    )
    group.add_argument(
        "--check",
        type=Path,
        help="fail unless this file is the current canonical catalog",
    )
    parser.add_argument("--manifest-root", type=Path, default=DEFAULT_MANIFEST_ROOT)
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    try:
        generated = catalog_bytes(arguments.manifest_root)
        if arguments.check is not None:
            current = _read_bounded_regular_file(
                arguments.check, max_bytes=CATALOG_MAX_BYTES
            )
            if current != generated:
                _fail(
                    f"{arguments.check} is stale; run "
                    "python -m scripts.build_catalog --output index.json"
                )
        elif arguments.output is not None:
            _write_atomic(arguments.output, generated)
        else:
            sys.stdout.buffer.write(generated)
    except CatalogBuildError as exception:
        print(f"catalog validation failed: {exception}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
