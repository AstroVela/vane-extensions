from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

import httpx
from packaging.requirements import Requirement

from scripts.build_catalog import DEFAULT_MANIFEST_ROOT, PROJECT_ROOT, load_manifests
from scripts.build_site import (
    MetadataClient,
    SiteBuildError,
    _detail_html,
    _requirement_for_base_install,
    assemble_site,
    build_details,
)

GENERATED_AT = "2026-09-02T12:00:00Z"


class _FakeMetadataClient:
    def __init__(self, responses: Mapping[str, object | None]) -> None:
        self.responses = dict(responses)
        self.requests: list[tuple[str, Mapping[str, str] | None, bool]] = []

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> object | None:
        self.requests.append((url, headers, allow_not_found))
        if url not in self.responses:
            raise AssertionError(f"unexpected metadata URL: {url}")
        value = self.responses[url]
        if value is None and not allow_not_found:
            raise AssertionError(f"unexpected missing metadata: {url}")
        return value


class MetadataClientTests(unittest.TestCase):
    def test_not_found_can_be_reported_without_following_redirects(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/missing":
                return httpx.Response(404)
            return httpx.Response(
                302, headers={"Location": "https://example.com/missing"}
            )

        with MetadataClient(transport=httpx.MockTransport(handler)) as client:
            self.assertIsNone(
                client.get_json("https://example.com/missing", allow_not_found=True)
            )
            with self.assertRaisesRegex(SiteBuildError, "HTTP 302"):
                client.get_json("https://example.com/redirect")

    def test_response_size_limit_is_applied_after_decoding(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"12345")
        )

        with (
            patch("scripts.build_site.REMOTE_METADATA_MAX_BYTES", 4),
            MetadataClient(transport=transport) as client,
            self.assertRaisesRegex(SiteBuildError, "size limit"),
        ):
            client.get_json("https://example.com/data")

    def test_duplicate_json_keys_are_rejected(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b'{"value": 1, "value": 2}')
        )

        with MetadataClient(transport=transport) as client:
            with self.assertRaisesRegex(SiteBuildError, "repeats key"):
                client.get_json("https://example.com/data")


def _github_response(repository: str, stars: int = 7) -> dict[str, object]:
    return {
        "full_name": repository.removeprefix("https://github.com/"),
        "html_url": repository,
        "stargazers_count": stars,
    }


def _package_response(
    distribution_name: str,
    version: str = "0.2.0",
    *,
    requires_dist: list[str] | None = None,
) -> dict[str, object]:
    wheel_distribution = distribution_name.replace("-", "_")
    return {
        "info": {
            "name": distribution_name,
            "version": version,
            "requires_python": ">=3.10,<3.15",
            "requires_dist": requires_dist or [],
        },
        "urls": [
            {
                "packagetype": "bdist_wheel",
                "yanked": False,
                "filename": (
                    f"{wheel_distribution}-{version}-cp310-none-"
                    "manylinux_2_28_x86_64.whl"
                ),
                "upload_time_iso_8601": "2026-09-02T11:00:00Z",
                "url": "https://files.example/artifact.whl",
                "digests": {"sha256": "not-published-by-the-registry"},
            },
            {
                "packagetype": "bdist_wheel",
                "yanked": False,
                "filename": (
                    f"{wheel_distribution}-{version}-cp314-none-"
                    "manylinux_2_28_x86_64.whl"
                ),
                "upload_time_iso_8601": "2026-09-02T11:01:00Z",
            },
            {
                "packagetype": "sdist",
                "yanked": False,
                "upload_time_iso_8601": "2026-09-02T11:02:00Z",
            },
        ],
    }


def _release_response(
    distribution_name: str, version: str, requires_dist: list[str]
) -> dict[str, object]:
    return _package_response(
        distribution_name, version, requires_dist=requires_dist
    )


def _sdist_only_response(
    distribution_name: str,
    version: str = "0.2.0",
    *,
    requires_dist: list[str] | None = None,
) -> dict[str, object]:
    response = _package_response(
        distribution_name, version, requires_dist=requires_dist
    )
    response["urls"] = [
        {
            "packagetype": "sdist",
            "yanked": False,
            "upload_time_iso_8601": "2026-09-02T11:02:00Z",
        }
    ]
    return response


def _responses_for_checked_in_manifests() -> dict[str, object | None]:
    responses: dict[str, object | None] = {}
    for manifest in load_manifests(DEFAULT_MANIFEST_ROOT):
        repository = str(manifest["repository"])
        distribution = str(manifest["distribution_name"])
        responses[
            f"https://api.github.com/repos/{repository.removeprefix('https://github.com/')}"
        ] = _github_response(repository)
        responses[f"https://test.pypi.org/pypi/{distribution}/json"] = (
            None
            if manifest["extension_name"] == "lance"
            else _package_response(distribution)
        )
    return responses


def _checked_in_manifest(extension_name: str) -> dict[str, object]:
    return next(
        manifest
        for manifest in load_manifests(DEFAULT_MANIFEST_ROOT)
        if manifest["extension_name"] == extension_name
    )


class BuildSiteTests(unittest.TestCase):
    def test_empty_extra_marker_reduction_is_platform_independent(self) -> None:
        self.assertIsNone(
            _requirement_for_base_install(
                Requirement(
                    'vane-extension-optional===1; extra == "a" or extra == "b"'
                )
            )
        )
        self.assertIsNone(
            _requirement_for_base_install(
                Requirement('vane-extension-optional===1; extra not in "docs"')
            )
        )
        self.assertIsNone(
            _requirement_for_base_install(
                Requirement(
                    'vane-extension-optional===1; extra == "a" '
                    'and sys_platform == "linux"'
                )
            )
        )
        mixed_requirement = Requirement(
            'vane-extension-optional===1; extra == "a" '
            'or python_version < "3.11"'
        )
        mixed_base_requirement = _requirement_for_base_install(mixed_requirement)
        self.assertIsNotNone(mixed_base_requirement)
        self.assertEqual(
            str(mixed_base_requirement),
            'vane-extension-optional===1; python_version < "3.11"',
        )
        platform_requirement = Requirement(
            'vane-extension-required===1; sys_platform == "extra"'
        )
        self.assertIs(
            _requirement_for_base_install(platform_requirement),
            platform_requirement,
        )
        base_requirement = _requirement_for_base_install(
            Requirement('vane-ai===1; extra != "docs"')
        )
        self.assertIsNotNone(base_requirement)
        self.assertEqual(str(base_requirement), "vane-ai===1")
        self.assertIsNone(base_requirement.marker)
        base_platform_requirement = _requirement_for_base_install(
            Requirement(
                'vane-ai===1; extra != "docs" and sys_platform == "linux"'
            )
        )
        self.assertIsNotNone(base_platform_requirement)
        self.assertEqual(
            str(base_platform_requirement),
            'vane-ai===1; sys_platform == "linux"',
        )
        base_tautology_requirement = _requirement_for_base_install(
            Requirement(
                'vane-ai===1; extra == "docs" or extra != "docs"'
            )
        )
        self.assertIsNotNone(base_tautology_requirement)
        self.assertEqual(str(base_tautology_requirement), "vane-ai===1")

    def test_build_details_enriches_without_exposing_artifact_locations(self) -> None:
        details = build_details(
            generated_at=GENERATED_AT,
            client=_FakeMetadataClient(_responses_for_checked_in_manifests()),
            github_token="test-token",
        )

        iceberg = next(
            detail for detail in details if detail["extension_name"] == "iceberg"
        )
        self.assertEqual(iceberg["source"], {"github_stars": 7})
        self.assertEqual(iceberg["package"]["python_versions"], ["3.10", "3.14"])
        self.assertEqual(
            iceberg["package"]["platform_tags"], ["manylinux_2_28_x86_64"]
        )
        self.assertEqual(iceberg["package"]["abi_tags"], ["none"])
        self.assertEqual(iceberg["package"]["wheel_count"], 2)
        self.assertEqual(
            iceberg["package"]["latest_release_uploaded_at"],
            "2026-09-02T11:02:00Z",
        )
        serialized = json.dumps(iceberg)
        self.assertNotIn("files.example", serialized)
        self.assertNotIn("not-published-by-the-registry", serialized)

    def test_missing_package_is_reported_without_cross_index_fallback(self) -> None:
        client = _FakeMetadataClient(_responses_for_checked_in_manifests())

        details = build_details(generated_at=GENERATED_AT, client=client)

        lance = next(
            detail for detail in details if detail["extension_name"] == "lance"
        )
        self.assertFalse(lance["package"]["published"])
        self.assertIsNone(lance["package"]["latest_version"])
        self.assertEqual(lance["installation"]["install_commands"], [])
        lance_requests = [
            url for url, _headers, _missing in client.requests if "lance" in url
        ]
        self.assertIn(
            "https://test.pypi.org/pypi/vane-extension-lance/json", lance_requests
        )
        self.assertFalse(any("https://pypi.org/" in url for url in lance_requests))

    def test_assemble_site_preserves_catalog_and_writes_details_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "site"

            details = assemble_site(
                output,
                generated_at=GENERATED_AT,
                client=_FakeMetadataClient(_responses_for_checked_in_manifests()),
            )

            self.assertEqual(
                (output / "v1" / "index.json").read_bytes(),
                (PROJECT_ROOT / "index.json").read_bytes(),
            )
            self.assertTrue(
                (output / "extensions" / "iceberg" / "index.html").is_file()
            )
            aggregate = json.loads(
                (output / "v1" / "extensions" / "index.json").read_text()
            )
            self.assertEqual(len(aggregate["extensions"]), len(details))
            self.assertNotIn("documentation", aggregate["extensions"][0])
            self.assertEqual(
                json.loads(
                    (output / "v1" / "extensions" / "iceberg.json").read_text()
                )["$schema"],
                "../../schema/extension-detail.schema.json",
            )
            metrics = json.loads(
                (output / "v1" / "metrics" / "downloads-last-week.json").read_text()
            )
            self.assertTrue(
                all(
                    entry["downloads_last_week"] is None
                    for entry in metrics["extensions"]
                )
            )

    def test_pypi_package_gets_separate_download_metrics(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        manifest["package_index"] = "pypi"
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        responses = {
            (
                "https://api.github.com/repos/"
                f"{repository.removeprefix('https://github.com/')}"
            ): _github_response(repository),
            f"https://pypi.org/pypi/{distribution}/json": _package_response(
                distribution
            ),
            f"https://pypistats.org/api/packages/{distribution}/recent": {
                "data": {"last_week": 123}
            },
        }

        detail = build_details(
            manifest_root=self._single_manifest_root(manifest),
            generated_at=GENERATED_AT,
            client=_FakeMetadataClient(responses),
        )[0]

        self.assertEqual(
            detail["metrics"],
            {"downloads_last_week": 123, "source": "pypistats.org"},
        )
        self.assertEqual(
            detail["installation"]["install_commands"],
            [
                "python -m pip --isolated install "
                "--index-url https://pypi.org/simple/ "
                f"{distribution}===0.2.0"
            ],
        )

    def test_testpypi_installation_keeps_indexes_isolated(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        provider_version = "0.2.0"
        vane_version = "0.2.0.dev1"
        avro_version = "0.2.0"
        optional_version = "1.0"
        responses = {
            (
                "https://api.github.com/repos/"
                f"{repository.removeprefix('https://github.com/')}"
            ): _github_response(repository),
            f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                distribution,
                provider_version,
                requires_dist=[
                    f'vane-ai==={vane_version}; extra != "docs"',
                    f"vane-extension-avro==={avro_version}",
                    (
                        f"vane-extension-optional==={optional_version}; "
                        'extra == "a" or extra == "b"'
                    ),
                    (
                        "root-sdk @ https://example.invalid/root.whl ; "
                        'extra == "sdk"'
                    ),
                ],
            ),
            f"https://test.pypi.org/pypi/vane-ai/{vane_version}/json": (
                _release_response(
                    "vane-ai",
                    vane_version,
                    [
                        "numpy>=2",
                        "typing-extensions",
                        'optional-sdk; extra == "openai"',
                        (
                            "optional-url @ https://example.invalid/sdk.whl ; "
                            'extra == "sdk"'
                        ),
                        'platform-marker; sys_platform == "extra"',
                        'default-extra; extra != "openai"',
                    ],
                )
            ),
            f"https://test.pypi.org/pypi/vane-extension-avro/{avro_version}/json": (
                _release_response(
                    "vane-extension-avro",
                    avro_version,
                    [f'vane-ai==={vane_version}; extra != "docs"'],
                )
            ),
        }

        detail = build_details(
            manifest_root=self._single_manifest_root(manifest),
            generated_at=GENERATED_AT,
            client=_FakeMetadataClient(responses),
        )[0]

        public_command, testpypi_command = detail["installation"][
            "install_commands"
        ]
        self.assertEqual(
            detail["package"]["requires_dist"],
            [
                f'vane-ai==={vane_version}; extra != "docs"',
                f"vane-extension-avro==={avro_version}",
                (
                    f"vane-extension-optional==={optional_version}; "
                    'extra == "a" or extra == "b"'
                ),
            ],
        )
        self.assertIn("--index-url https://pypi.org/simple/", public_command)
        self.assertIn("python -m pip --isolated install", public_command)
        self.assertIn("'numpy>=2'", public_command)
        self.assertIn("typing-extensions", public_command)
        self.assertIn("platform-marker", public_command)
        self.assertIn("default-extra", public_command)
        self.assertNotIn("test.pypi.org", public_command)
        self.assertNotIn("vane-", public_command)
        self.assertNotIn("optional-sdk", public_command)
        self.assertNotIn("optional-url", public_command)
        self.assertNotIn("--force-reinstall", public_command)
        self.assertIn("--force-reinstall", testpypi_command)
        self.assertIn("python -m pip --isolated install", testpypi_command)
        self.assertIn("--no-deps", testpypi_command)
        self.assertIn("--only-binary=:all:", testpypi_command)
        self.assertIn("--index-url https://test.pypi.org/simple/", testpypi_command)
        self.assertIn(f"vane-ai==={vane_version}", testpypi_command)
        self.assertIn(f"vane-extension-avro==={avro_version}", testpypi_command)
        self.assertIn(f"{distribution}==={provider_version}", testpypi_command)
        self.assertNotIn("vane-extension-optional", testpypi_command)
        self.assertNotIn("https://pypi.org/simple/", testpypi_command)
        self.assertNotIn(
            "--extra-index-url", "\n".join(detail["installation"]["install_commands"])
        )
        self.assertNotIn("example.invalid", json.dumps(detail))

    def test_testpypi_vane_dependencies_must_use_exact_versions(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        responses = {
            (
                "https://api.github.com/repos/"
                f"{repository.removeprefix('https://github.com/')}"
            ): _github_response(repository),
            f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                distribution,
                requires_dist=["vane-ai>=0.2"],
            ),
        }

        with self.assertRaisesRegex(SiteBuildError, "one exact"):
            build_details(
                manifest_root=self._single_manifest_root(manifest),
                generated_at=GENERATED_AT,
                client=_FakeMetadataClient(responses),
            )

    def test_testpypi_recipe_rejects_active_direct_url_dependencies(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        responses = {
            (
                "https://api.github.com/repos/"
                f"{repository.removeprefix('https://github.com/')}"
            ): _github_response(repository),
            f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                distribution,
                requires_dist=[
                    "sdk @ https://example.invalid/sdk.whl ; extra != 'docs'"
                ],
            ),
        }

        with self.assertRaisesRegex(SiteBuildError, "unsupported direct URL"):
            build_details(
                manifest_root=self._single_manifest_root(manifest),
                generated_at=GENERATED_AT,
                client=_FakeMetadataClient(responses),
            )

    def test_testpypi_recipe_requires_wheels_for_entire_vane_closure(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        vane_version = "0.2.0.dev1"
        github_url = (
            "https://api.github.com/repos/"
            f"{repository.removeprefix('https://github.com/')}"
        )
        package_url = f"https://test.pypi.org/pypi/{distribution}/json"
        release_url = f"https://test.pypi.org/pypi/vane-ai/{vane_version}/json"
        cases = {
            "root": {
                github_url: _github_response(repository),
                package_url: _sdist_only_response(distribution),
            },
            "dependency": {
                github_url: _github_response(repository),
                package_url: _package_response(
                    distribution, requires_dist=[f"vane-ai==={vane_version}"]
                ),
                release_url: _sdist_only_response("vane-ai", vane_version),
            },
        }

        for case, responses in cases.items():
            with (
                self.subTest(case=case),
                self.assertRaisesRegex(SiteBuildError, "non-yanked wheel"),
            ):
                build_details(
                    manifest_root=self._single_manifest_root(manifest),
                    generated_at=GENERATED_AT,
                    client=_FakeMetadataClient(responses),
                )

    def test_detail_html_escapes_reviewed_text(self) -> None:
        detail = next(
            detail
            for detail in build_details(
                generated_at=GENERATED_AT,
                client=_FakeMetadataClient(_responses_for_checked_in_manifests()),
            )
            if detail["extension_name"] == "iceberg"
        )
        detail["documentation"] = {
            **detail["documentation"],
            "extended_description": "<script>alert(1)</script>",
        }

        page = _detail_html(detail)

        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_existing_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)

            with self.assertRaisesRegex(SiteBuildError, "already exists"):
                assemble_site(
                    output,
                    generated_at=GENERATED_AT,
                    client=_FakeMetadataClient({}),
                )

    def _single_manifest_root(self, manifest: Mapping[str, object]) -> Path:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        root = Path(temporary_directory.name)
        extension_name = str(manifest["extension_name"])
        directory = root / extension_name
        directory.mkdir()
        (directory / "extension.json").write_text(
            json.dumps({"$schema": "../../schema/extension.schema.json", **manifest}),
            encoding="utf-8",
        )
        return root


if __name__ == "__main__":
    unittest.main()
