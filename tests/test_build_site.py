from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

import httpx
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts.build_catalog import DEFAULT_MANIFEST_ROOT, PROJECT_ROOT, load_manifests
from scripts.build_site import (
    MetadataClient,
    SiteBuildError,
    _detail_html,
    _powershell_install_script,
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


class _FakeInstallationResolver:
    def __init__(self, resolved: tuple[str, ...] | None = None) -> None:
        self.resolved = resolved
        self.calls: list[dict[str, object]] = []

    def resolve(self, **arguments: object) -> dict[str, tuple[str, ...]]:
        self.calls.append(arguments)
        pins = self.resolved or (
            f"{arguments['distribution_name']}=={arguments['version']}",
        )
        result: dict[str, list[str]] = {"pypi": [], "testpypi": []}
        for pin in pins:
            name = canonicalize_name(Requirement(pin).name)
            index = (
                str(arguments["package_index"]) if name.startswith("vane-") else "pypi"
            )
            result[index].append(pin)
        return {index: tuple(pins) for index, pins in result.items()}


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
    requires_python: str | None = ">=3.10,<3.15",
    wheel_tags: tuple[str, ...] | None = None,
    file_requires_python: tuple[str | None, ...] | None = None,
) -> dict[str, object]:
    wheel_distribution = distribution_name.replace("-", "_")
    selected_wheel_tags = (
        wheel_tags
        if wheel_tags is not None
        else (
            "cp310-none-manylinux_2_28_x86_64",
            "cp314-none-manylinux_2_28_x86_64",
        )
    )
    return {
        "info": {
            "name": distribution_name,
            "version": version,
            "requires_python": requires_python,
            "requires_dist": requires_dist or [],
        },
        "urls": [
            *[
                {
                    "packagetype": "bdist_wheel",
                    "yanked": False,
                    "filename": f"{wheel_distribution}-{version}-{wheel_tag}.whl",
                    **(
                        {"requires_python": file_requires_python[index]}
                        if file_requires_python is not None
                        else {}
                    ),
                    "upload_time_iso_8601": (f"2026-09-02T11:{index:02d}:00Z"),
                    **(
                        {
                            "url": "https://files.example/artifact.whl",
                            "digests": {"sha256": "not-published-by-the-registry"},
                        }
                        if index == 0
                        else {}
                    ),
                }
                for index, wheel_tag in enumerate(selected_wheel_tags)
            ],
            {
                "packagetype": "sdist",
                "yanked": False,
                "upload_time_iso_8601": (
                    f"2026-09-02T11:{len(selected_wheel_tags):02d}:00Z"
                ),
            },
        ],
    }


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
    def setUp(self) -> None:
        resolver = patch(
            "scripts.build_site.PdmInstallationResolver",
            side_effect=_FakeInstallationResolver,
        )
        resolver.start()
        self.addCleanup(resolver.stop)

    def test_powershell_install_script_restores_environment_and_guards_steps(
        self,
    ) -> None:
        script = _powershell_install_script(
            [
                ["python", 'package==1; platform_machine == "O\'Reilly"'],
                ["python", "provider==1"],
            ]
        )

        self.assertIsNotNone(script)
        assert script is not None
        self.assertTrue(script.startswith("& {\n"))
        self.assertIn(
            "$__vanePipConfigFileWasSet = Test-Path Env:PIP_CONFIG_FILE", script
        )
        self.assertIn("$__vanePipConfigFile = $env:PIP_CONFIG_FILE", script)
        self.assertIn("$env:PIP_CONFIG_FILE = 'nul'", script)
        self.assertLess(
            script.index("$__vanePipConfigFile = $env:PIP_CONFIG_FILE"),
            script.index("$env:PIP_CONFIG_FILE = 'nul'"),
        )
        self.assertIn("'package==1; platform_machine == \"O''Reilly\"'", script)
        self.assertEqual(script.count("if (-not $__vanePipSucceeded"), 2)
        first_guard = script.index("if (-not $__vanePipSucceeded")
        self.assertLess(script.index("'package==1"), first_guard)
        self.assertLess(first_guard, script.index("'provider==1'"))
        self.assertIn("} finally {", script)
        self.assertIn("$env:PIP_CONFIG_FILE = $__vanePipConfigFile", script)
        self.assertIn(
            "Remove-Item Env:PIP_CONFIG_FILE -ErrorAction SilentlyContinue",
            script,
        )

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
        self.assertEqual(iceberg["package"]["platform_tags"], ["manylinux_2_28_x86_64"])
        self.assertEqual(iceberg["package"]["abi_tags"], ["none"])
        self.assertEqual(iceberg["package"]["wheel_count"], 2)
        self.assertEqual(
            iceberg["package"]["latest_release_uploaded_at"],
            "2026-09-02T11:02:00Z",
        )
        serialized = json.dumps(iceberg)
        self.assertNotIn("files.example", serialized)
        self.assertNotIn("not-published-by-the-registry", serialized)

    def test_github_repository_casing_does_not_change_identity(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        canonical_repository = str(manifest["repository"])
        slug = canonical_repository.removeprefix("https://github.com/")
        for manifest_slug in (slug.lower(), slug.upper(), slug.swapcase()):
            with self.subTest(manifest_slug=manifest_slug):
                manifest["repository"] = f"https://github.com/{manifest_slug}"
                responses = {
                    f"https://api.github.com/repos/{manifest_slug}": _github_response(
                        canonical_repository
                    ),
                    f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                        distribution
                    ),
                }
                detail = build_details(
                    manifest_root=self._single_manifest_root(manifest),
                    generated_at=GENERATED_AT,
                    client=_FakeMetadataClient(responses),
                )[0]
                self.assertEqual(detail["source"], {"github_stars": 7})
                self.assertEqual(detail["repository"], manifest["repository"])

    def test_github_metadata_rejects_mismatched_or_noncanonical_identity(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        slug = repository.removeprefix("https://github.com/")
        invalid_values = (
            ("full_name", "another-owner/another-repo"),
            ("full_name", slug.replace("AstroVela", "AſtroVela")),
            ("html_url", "https://github.com/another-owner/another-repo"),
            ("html_url", repository.replace("https:", "http:")),
            ("html_url", repository.replace("https:", "httpſ:")),
            ("html_url", repository.replace("github.com", "github.example")),
            ("html_url", repository.replace("github.com", "user@github.com")),
            ("html_url", repository.replace("github.com", "github.com:443")),
            ("html_url", f"{repository}?tab=readme-ov-file"),
            ("html_url", f"{repository}#readme"),
            ("html_url", f"{repository}/"),
            ("html_url", None),
        )
        for field, value in invalid_values:
            with self.subTest(field=field, value=value):
                github_response = _github_response(repository)
                github_response[field] = value
                responses = {
                    f"https://api.github.com/repos/{slug}": github_response,
                    f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                        distribution
                    ),
                }
                with self.assertRaises(SiteBuildError):
                    build_details(
                        manifest_root=self._single_manifest_root(manifest),
                        generated_at=GENERATED_AT,
                        client=_FakeMetadataClient(responses),
                    )

    def test_generic_python_wheel_tags_preserve_exact_and_broad_support(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        package = _package_response(distribution)
        package_urls = package["urls"]
        assert isinstance(package_urls, list)
        first_wheel = package_urls[0]
        assert isinstance(first_wheel, dict)
        first_wheel["filename"] = (
            f"{distribution.replace('-', '_')}-0.2.0-py310-none-any.whl"
        )
        broad_wheel = dict(first_wheel)
        broad_wheel["filename"] = (
            f"{distribution.replace('-', '_')}-0.2.0-py3-none-any.whl"
        )
        package_urls.insert(1, broad_wheel)
        responses = {
            (
                "https://api.github.com/repos/"
                f"{repository.removeprefix('https://github.com/')}"
            ): _github_response(repository),
            f"https://test.pypi.org/pypi/{distribution}/json": package,
        }

        detail = build_details(
            manifest_root=self._single_manifest_root(manifest),
            generated_at=GENERATED_AT,
            client=_FakeMetadataClient(responses),
        )[0]

        self.assertEqual(detail["package"]["python_versions"], ["3.10", "3.14"])
        self.assertEqual(detail["package"]["python_tags"], ["cp314", "py3", "py310"])
        landing_page = (PROJECT_ROOT / "site" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("pythonCompatibility", landing_page)
        self.assertIn("entry.package.python_versions.join", landing_page)
        self.assertIn("entry.package.python_tags.join", landing_page)

    def test_missing_package_is_reported_without_cross_index_fallback(self) -> None:
        client = _FakeMetadataClient(_responses_for_checked_in_manifests())

        details = build_details(generated_at=GENERATED_AT, client=client)

        lance = next(
            detail for detail in details if detail["extension_name"] == "lance"
        )
        self.assertFalse(lance["package"]["published"])
        self.assertIsNone(lance["package"]["latest_version"])
        self.assertIsNone(lance["installation"]["posix_install_script"])
        self.assertIsNone(lance["installation"]["powershell_install_script"])
        lance_requests = [
            url for url, _headers, _missing in client.requests if "lance" in url
        ]
        self.assertIn(
            "https://test.pypi.org/pypi/vane-extension-lance/json", lance_requests
        )
        self.assertFalse(any("https://pypi.org/" in url for url in lance_requests))

    def test_assemble_site_preserves_catalog_and_writes_details_and_metrics(
        self,
    ) -> None:
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
                json.loads((output / "v1" / "extensions" / "iceberg.json").read_text())[
                    "$schema"
                ],
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
        resolver = _FakeInstallationResolver(
            ("public-transitive==1.2.3", f"{distribution}==0.2.0")
        )

        detail = build_details(
            manifest_root=self._single_manifest_root(manifest),
            generated_at=GENERATED_AT,
            client=_FakeMetadataClient(responses),
            resolver=resolver,
        )[0]

        self.assertEqual(
            detail["metrics"],
            {"downloads_last_week": 123, "source": "pypistats.org"},
        )
        self.assertEqual(
            detail["installation"]["posix_install_script"],
            "env PIP_CONFIG_FILE=/dev/null "
            "python -m pip --isolated install "
            "--force-reinstall --no-deps --only-binary=:all: "
            "--index-url https://pypi.org/simple/ "
            f"public-transitive==1.2.3 {distribution}==0.2.0 &&\n"
            "env PIP_CONFIG_FILE=/dev/null python -m pip --isolated check",
        )
        powershell_script = detail["installation"]["powershell_install_script"]
        self.assertIsInstance(powershell_script, str)
        assert isinstance(powershell_script, str)
        self.assertIn(
            "'--index-url' 'https://pypi.org/simple/' "
            f"'public-transitive==1.2.3' '{distribution}==0.2.0'",
            powershell_script,
        )
        self.assertEqual(resolver.calls[0]["distribution_name"], distribution)
        self.assertEqual(resolver.calls[0]["version"], "0.2.0")
        self.assertEqual(resolver.calls[0]["requires_python"], ">=3.10,<3.15")
        self.assertEqual(resolver.calls[0]["package_index"], "pypi")

    def test_optional_download_metrics_do_not_block_a_published_provider(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        manifest["package_index"] = "pypi"
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        slug = repository.removeprefix("https://github.com/")
        resolver = _FakeInstallationResolver((f"{distribution}==0.2.0",))
        responses = {
            f"https://api.github.com/repos/{slug}": _github_response(repository),
            f"https://pypi.org/pypi/{distribution}/json": _package_response(
                distribution
            ),
        }
        outcomes = {
            "timeout": httpx.ReadTimeout("metrics timeout"),
            "connection-error": httpx.ConnectError("metrics offline"),
            "rate-limit": httpx.Response(429),
            "server-error": httpx.Response(503),
            "invalid-json": httpx.Response(200, content=b"{"),
            "missing-data": httpx.Response(200, json={}),
            "invalid-data": httpx.Response(200, json={"data": []}),
            "missing-count": httpx.Response(200, json={"data": {}}),
            "negative-count": httpx.Response(200, json={"data": {"last_week": -1}}),
            "boolean-count": httpx.Response(200, json={"data": {"last_week": True}}),
            "missing-metrics": httpx.Response(404),
        }
        for case, outcome in outcomes.items():

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.host == "pypistats.org":
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                return httpx.Response(200, json=responses[str(request.url)])

            log_check = (
                self.assertNoLogs("scripts.build_site", level="WARNING")
                if case == "missing-metrics"
                else self.assertLogs("scripts.build_site", level="WARNING")
            )
            with (
                self.subTest(case=case),
                MetadataClient(transport=httpx.MockTransport(handler)) as client,
                log_check,
            ):
                detail = build_details(
                    manifest_root=self._single_manifest_root(manifest),
                    generated_at=GENERATED_AT,
                    client=client,
                    resolver=resolver,
                )[0]
                self.assertEqual(
                    detail["metrics"], {"downloads_last_week": None, "source": None}
                )
                self.assertIsNotNone(detail["installation"]["posix_install_script"])
                self.assertIn("Unavailable (pypi)", _detail_html(detail))

    def test_optional_metrics_do_not_hide_required_metadata_failures(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        manifest["package_index"] = "pypi"
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        slug = repository.removeprefix("https://github.com/")
        github_url = f"https://api.github.com/repos/{slug}"
        package_url = f"https://pypi.org/pypi/{distribution}/json"
        responses = {
            github_url: _github_response(repository),
            package_url: _package_response(distribution),
        }
        for failed_url in (github_url, package_url):

            def handler(request: httpx.Request) -> httpx.Response:
                if (
                    str(request.url) == failed_url
                    or request.url.host == "pypistats.org"
                ):
                    return httpx.Response(503)
                return httpx.Response(200, json=responses[str(request.url)])

            with (
                self.subTest(failed_url=failed_url),
                MetadataClient(transport=httpx.MockTransport(handler)) as client,
                patch("scripts.build_site._LOGGER.warning"),
                self.assertRaisesRegex(SiteBuildError, "HTTP 503"),
            ):
                build_details(
                    manifest_root=self._single_manifest_root(manifest),
                    generated_at=GENERATED_AT,
                    client=client,
                )

    def test_pdm_pins_are_split_by_source_without_rewriting_markers(self) -> None:
        resolver = _FakeInstallationResolver(
            (
                'public-wheel==1; platform_machine == "AMD64"',
                "vane-ai==0.2.0",
                "vane-extension-iceberg==0.2.0",
            )
        )
        detail = build_details(
            manifest_root=self._single_manifest_root(_checked_in_manifest("iceberg")),
            generated_at=GENERATED_AT,
            client=_FakeMetadataClient(_responses_for_checked_in_manifests()),
            resolver=resolver,
        )[0]
        commands = detail["installation"]["posix_install_script"].split(" &&\n")
        public, internal, check = map(shlex.split, commands)
        self.assertIn('public-wheel==1; platform_machine == "AMD64"', public)
        self.assertIn("https://pypi.org/simple/", public)
        self.assertIn("https://test.pypi.org/simple/", internal)
        self.assertNotIn("vane-ai==0.2.0", public)
        self.assertIn("vane-ai==0.2.0", internal)
        for arguments in (public, internal):
            for option in (
                "--isolated",
                "--no-deps",
                "--only-binary=:all:",
                "--force-reinstall",
            ):
                self.assertIn(option, arguments)
            self.assertNotIn("--extra-index-url", arguments)
        self.assertEqual(check[-1], "check")

    def test_wheel_tags_are_informational_not_a_cross_platform_model(self) -> None:
        manifest = _checked_in_manifest("iceberg")
        responses = _responses_for_checked_in_manifests()
        responses["https://test.pypi.org/pypi/vane-extension-iceberg/json"] = (
            _package_response(
                "vane-extension-iceberg",
                wheel_tags=("cp314-none-android_24_arm64_v8a", "cp310-none-win32"),
                file_requires_python=(">=3.14", "<3.11"),
                requires_dist=["hidden @ https://user:secret@example.com/file.whl"],
            )
        )
        detail = build_details(
            manifest_root=self._single_manifest_root(manifest),
            generated_at=GENERATED_AT,
            client=_FakeMetadataClient(responses),
        )[0]
        self.assertEqual(
            detail["package"]["platform_tags"], ["android_24_arm64_v8a", "win32"]
        )
        self.assertNotIn("secret", json.dumps(detail))
        self.assertIn("upstream", _detail_html(detail).lower())

    def test_published_provider_requires_wheels_and_python_metadata(self) -> None:
        for values in ({"wheel_tags": ()}, {"requires_python": None}):
            with self.subTest(values=values):
                responses = _responses_for_checked_in_manifests()
                responses["https://test.pypi.org/pypi/vane-extension-iceberg/json"] = (
                    _package_response("vane-extension-iceberg", **values)
                )
                with self.assertRaises(SiteBuildError):
                    build_details(
                        manifest_root=self._single_manifest_root(
                            _checked_in_manifest("iceberg")
                        ),
                        generated_at=GENERATED_AT,
                        client=_FakeMetadataClient(responses),
                    )

    def test_compressed_wheel_tags_are_bounded_before_filename_parsing(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        responses = {
            (
                "https://api.github.com/repos/"
                f"{repository.removeprefix('https://github.com/')}"
            ): _github_response(repository),
            f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                distribution, wheel_tags=("py2.py3-none-any",)
            ),
        }
        with (
            patch("scripts.build_site.PACKAGE_WHEEL_TAGS_MAX_COUNT", 1),
            patch("scripts.build_site.parse_wheel_filename") as parse_filename,
            self.assertRaisesRegex(SiteBuildError, "invalid wheel filename"),
        ):
            build_details(
                manifest_root=self._single_manifest_root(manifest),
                generated_at=GENERATED_AT,
                client=_FakeMetadataClient(responses),
            )
        parse_filename.assert_not_called()

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
