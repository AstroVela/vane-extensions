from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import tomllib
import unittest
import zipfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import httpx
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts.build_catalog import DEFAULT_MANIFEST_ROOT, PROJECT_ROOT, load_manifests
from scripts.build_site import (
    MetadataClient,
    SiteBuildError,
    UvPublicDependencyResolver,
    _abi_tags_overlap,
    _detail_html,
    _parse_public_lock,
    _platform_tags_overlap,
    _powershell_install_script,
    _pypi_install_arguments,
    _requirement_for_base_install,
    _resolver_python_lower_bound,
    _resolver_requirements,
    _wheel_platform_environment,
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


class _FakePublicDependencyResolver:
    def __init__(self, resolved: tuple[str, ...]) -> None:
        self.resolved = resolved
        self.calls: list[tuple[tuple[str, ...], str, str, bool]] = []

    def resolve(
        self,
        requirements: tuple[str, ...],
        *,
        requires_python: str,
        distribution_name: str,
        reject_vane: bool,
    ) -> tuple[str, ...]:
        self.calls.append(
            (requirements, requires_python, distribution_name, reject_vane)
        )
        return self.resolved


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


class PublicDependencyResolverTests(unittest.TestCase):
    def test_uv_preserves_the_full_python_range_without_using_the_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary)
            package_directory = index / "conditional-package"
            package_directory.mkdir()
            filename = "conditional_package-1.0-py3-none-any.whl"
            with zipfile.ZipFile(package_directory / filename, "w") as wheel:
                wheel.writestr(
                    "conditional_package-1.0.dist-info/METADATA",
                    "Metadata-Version: 2.3\nName: conditional-package\n"
                    "Version: 1.0\nRequires-Python: >=3.10,<3.12\n",
                )
                wheel.writestr(
                    "conditional_package-1.0.dist-info/WHEEL",
                    "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                )
            (package_directory / "index.html").write_text(
                f'<a href="{filename}">{filename}</a>', encoding="utf-8"
            )
            with patch.dict(
                "scripts.build_site._PACKAGE_INDEXES",
                {"pypi": {"simple": index.as_uri() + "/"}},
            ):
                locked = UvPublicDependencyResolver().resolve(
                    (
                        'conditional-package==1.0; python_version < "3.12"',
                        'missing-future-package==1.0; python_version >= "3.15"',
                        'missing-excluded-package==1.0; python_version == "3.13"',
                    ),
                    requires_python=">=3.10,!=3.13.*,<3.15",
                    distribution_name="vane-extension-test",
                    reject_vane=True,
                )
        self.assertEqual(len(locked), 1)
        requirement = Requirement(locked[0])
        self.assertEqual(requirement.name, "conditional-package")
        self.assertEqual(str(requirement.specifier), "==1.0")
        self.assertIsNotNone(requirement.marker)
        assert requirement.marker is not None
        for minor in range(10, 15):
            with self.subTest(minor=minor):
                self.assertEqual(
                    requirement.marker.evaluate(
                        {
                            "python_version": f"3.{minor}",
                            "python_full_version": f"3.{minor}.0",
                        }
                    ),
                    minor < 12,
                )

    def test_pypi_root_must_cover_every_supported_environment(self) -> None:
        name = "vane-extension-test"
        for condition in (None, 'python_full_version < "3.15"'):
            pin = f"{name}==1.0" + (f"; {condition}" if condition else "")
            with self.subTest(condition=condition):
                arguments = _pypi_install_arguments(
                    name,
                    "1.0",
                    {"wheel_count": 1, "requires_python": ">=3.10,<3.15"},
                    _FakeMetadataClient(_public_release_responses(f"{name}==1.0")),
                    _FakePublicDependencyResolver((pin,)),
                )
                self.assertEqual(arguments[-1], f"{name}==1.0")
        with self.assertRaisesRegex(SiteBuildError, "omits supported environments"):
            _pypi_install_arguments(
                name,
                "1.0",
                {"wheel_count": 1, "requires_python": ">=3.10,<3.15"},
                _FakeMetadataClient({}),
                _FakePublicDependencyResolver(
                    (f'{name}==1.0; python_full_version >= "3.12"',)
                ),
            )

    def test_python_lower_bound_comes_from_the_full_specifier_set(self) -> None:
        cases = {
            ">=3.10,<3.15": "3.10.0",
            ">=3.10,!=3.10.*,<3.15": "3.11.0",
            ">=3.10,!=3.11.*,<3.15": "3.10.0",
            "~=3.10.4": "3.10.4",
            ">3.10.4": "3.10.4",
            "==3.10.*": "3.10.0",
            "==3.10.4": "3.10.4",
            ">=3": "3.0.0",
        }
        for requires_python, expected in cases.items():
            with self.subTest(requires_python=requires_python):
                self.assertEqual(
                    _resolver_python_lower_bound(requires_python), expected
                )

    def test_pypi_wheels_must_share_an_environment_with_the_root(self) -> None:
        name = "vane-extension-test"
        pins = (f"{name}==1.0", "public-sdk==1.0")
        for public_tag, compatible in (
            ("py3-none-any", True),
            ("cp310-cp310-win_amd64", False),
        ):
            responses = {
                f"https://pypi.org/pypi/{name}/1.0/json": _release_response(
                    name,
                    "1.0",
                    [],
                    wheel_tags=("cp310-none-manylinux_2_28_x86_64",),
                ),
                "https://pypi.org/pypi/public-sdk/1.0/json": _release_response(
                    "public-sdk",
                    "1.0",
                    [],
                    wheel_tags=(public_tag,),
                ),
            }
            with self.subTest(public_tag=public_tag):
                arguments = (
                    name,
                    "1.0",
                    {"wheel_count": 1, "requires_python": ">=3.10,<3.15"},
                    _FakeMetadataClient(responses),
                    _FakePublicDependencyResolver(pins),
                )
                if compatible:
                    self.assertIn(
                        "public-sdk==1.0", _pypi_install_arguments(*arguments)
                    )
                else:
                    with self.assertRaisesRegex(
                        SiteBuildError, "no common environment"
                    ):
                        _pypi_install_arguments(*arguments)

    def test_pypi_wheel_validation_stays_within_declared_python_range(self) -> None:
        name = "vane-extension-test"
        pins = (f"{name}==1.0", 'public-sdk==1.0; python_version >= "3.10"')
        responses = {
            f"https://pypi.org/pypi/{name}/1.0/json": _release_response(
                name,
                "1.0",
                [],
                requires_python=None,
                wheel_tags=("py3-none-manylinux_2_28_x86_64",),
            ),
            "https://pypi.org/pypi/public-sdk/1.0/json": _release_response(
                "public-sdk",
                "1.0",
                [],
                wheel_tags=("cp310-cp310-win_amd64",),
            ),
        }
        with self.assertRaisesRegex(SiteBuildError, "no common environment"):
            _pypi_install_arguments(
                name,
                "1.0",
                {"wheel_count": 1, "requires_python": ">=3.10,<3.15"},
                _FakeMetadataClient(responses),
                _FakePublicDependencyResolver(pins),
            )

    def test_public_release_metadata_is_fetched_concurrently(self) -> None:
        name = "vane-extension-test"
        pins = (f"{name}==1.0", "public-sdk==1.0")
        barrier = Barrier(2)

        class ParallelClient(_FakeMetadataClient):
            def get_json(self, url: str, **kwargs: object) -> object:
                barrier.wait(timeout=5)
                return super().get_json(url, **kwargs)

        arguments = _pypi_install_arguments(
            name,
            "1.0",
            {"wheel_count": 1, "requires_python": ">=3.10,<3.15"},
            ParallelClient(_public_release_responses(*pins)),
            _FakePublicDependencyResolver(pins),
        )
        self.assertIn("public-sdk==1.0", arguments)

    def test_public_wheel_validation_does_not_reparse_unselected_extras(self) -> None:
        name = "vane-extension-test"
        pins = (f"{name}==1.0", "public-sdk==1.0")
        responses = _public_release_responses(*pins)
        responses["https://pypi.org/pypi/public-sdk/1.0/json"]["info"][
            "requires_dist"
        ] = [f'optional-{index}; extra == "unused"' for index in range(300)]
        arguments = _pypi_install_arguments(
            name,
            "1.0",
            {"wheel_count": 1, "requires_python": ">=3.10,<3.15"},
            _FakeMetadataClient(responses),
            _FakePublicDependencyResolver(pins),
        )
        self.assertIn("public-sdk==1.0", arguments)
        self.assertNotIn("optional-", " ".join(arguments))

    def test_public_sdist_only_release_is_rejected(self) -> None:
        name = "vane-extension-test"
        pins = (f"{name}==1.0", "public-sdk==1.0")
        responses = _public_release_responses(*pins)
        responses["https://pypi.org/pypi/public-sdk/1.0/json"] = _sdist_only_response(
            "public-sdk", "1.0"
        )
        with self.assertRaisesRegex(SiteBuildError, "non-yanked wheel"):
            _pypi_install_arguments(
                name,
                "1.0",
                {"wheel_count": 1, "requires_python": ">=3.10,<3.15"},
                _FakeMetadataClient(responses),
                _FakePublicDependencyResolver(pins),
            )

    def test_resolver_markers_preserve_compatible_release_precision(self) -> None:
        for requires_python, next_minor_supported in (
            ("~=3.10", True),
            ("~=3.10.4", False),
        ):
            with self.subTest(requires_python=requires_python):
                requirement = Requirement(
                    _resolver_requirements(("public-package==1",), requires_python)[0]
                )
                assert requirement.marker is not None
                self.assertEqual(
                    requirement.marker.evaluate({"python_full_version": "3.11.0"}),
                    next_minor_supported,
                )

    def test_unusable_python_lower_bounds_fail_before_running_uv(self) -> None:
        for requires_python in (
            "", "<3.15", ">=3.12,<3.10", ">=3.10rc1", ">=1!3.10"
        ):
            with (
                self.subTest(requires_python=requires_python),
                patch("scripts.build_site.subprocess.run") as run_mock,
                self.assertRaises(SiteBuildError),
            ):
                UvPublicDependencyResolver().resolve(
                    ("public-package>=1",),
                    requires_python=requires_python,
                    distribution_name="vane-extension-test",
                    reject_vane=True,
                )
            run_mock.assert_not_called()

    def test_lock_parser_accepts_only_exact_public_requirements(self) -> None:
        self.assertEqual(
            _parse_public_lock(
                b"",
                distribution_name="vane-extension-test",
                reject_vane=True,
            ),
            (),
        )
        self.assertEqual(
            _parse_public_lock(
                (
                    "Foo_Bar===1.0\n"
                    'conditional==2.0; python_version < "3.12"\n'
                ).encode(),
                distribution_name="vane-extension-test",
                reject_vane=True,
            ),
            (
                'conditional==2.0; python_version < "3.12"',
                "foo-bar==1.0",
            ),
        )
        self.assertEqual(
            _parse_public_lock(
                b"vane-ai==0.2.0\n",
                distribution_name="vane-ai",
                reject_vane=False,
            ),
            ("vane-ai==0.2.0",),
        )

    def test_lock_parser_rejects_unsafe_resolutions(self) -> None:
        cases = {
            "range": b"public-package>=1\n",
            "extra": b"public-package[feature]==1\n",
            "direct URL": b"public-package @ https://example.invalid/a.whl\n",
            "extra marker": b'public-package==1; extra == "feature"\n',
            "transitive Vane package": b"vane-ai==0.2.0\n",
        }
        for case, contents in cases.items():
            with self.subTest(case=case), self.assertRaises(SiteBuildError):
                _parse_public_lock(
                    contents,
                    distribution_name="vane-extension-test",
                    reject_vane=True,
                )

    def test_uv_resolution_is_config_free_bounded_and_cached(self) -> None:
        resolver = UvPublicDependencyResolver()

        def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            output_path = Path(arguments[arguments.index("--output-file") + 1])
            output_path.write_bytes(b"public-package==1.2.3\n")
            environment = kwargs["env"]
            self.assertIsInstance(environment, dict)
            assert isinstance(environment, dict)
            self.assertFalse(
                any(
                    key.upper().startswith(("PIP_", "UV_"))
                    for key in environment
                )
            )
            self.assertEqual(environment["HTTPS_PROXY"], "https://proxy.invalid")
            self.assertIn("--universal", arguments)
            self.assertEqual(
                arguments[arguments.index("--python-version") + 1], "3.10.0"
            )
            project = tomllib.loads(
                (Path(kwargs["cwd"]) / "pyproject.toml").read_text(encoding="utf-8")
            )
            dependency = Requirement(project["project"]["dependencies"][0])
            self.assertEqual(dependency.name, "public-package")
            assert dependency.marker is not None
            self.assertFalse(
                dependency.marker.evaluate({"python_full_version": "3.15.0"})
            )
            self.assertTrue(
                dependency.marker.evaluate({"python_full_version": "3.10.0"})
            )
            self.assertIn("--no-config", arguments)
            self.assertIn("--no-cache", arguments)
            self.assertIn("--only-binary=:all:", arguments)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            return subprocess.CompletedProcess(arguments, 0)

        with (
            patch.dict(
                os.environ,
                {
                    "PIP_INDEX_URL": "https://untrusted.invalid/simple",
                    "UV_INDEX": "https://untrusted.invalid/simple",
                    "HTTPS_PROXY": "https://proxy.invalid",
                },
            ),
            patch("scripts.build_site.subprocess.run", side_effect=run) as run_mock,
        ):
            first = resolver.resolve(
                ("public-package>=1",),
                requires_python=">=3.10,<3.15",
                distribution_name="vane-extension-test",
                reject_vane=True,
            )
            second = resolver.resolve(
                ("public-package>=1",),
                requires_python=">=3.10,<3.15",
                distribution_name="vane-extension-other",
                reject_vane=True,
            )

        self.assertEqual(first, ("public-package==1.2.3",))
        self.assertEqual(second, first)
        self.assertEqual(run_mock.call_count, 1)

    def test_cached_resolution_still_applies_the_vane_policy(self) -> None:
        resolver = UvPublicDependencyResolver()

        def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            output_path = Path(arguments[arguments.index("--output-file") + 1])
            output_path.write_bytes(b"vane-ai==0.2.0\n")
            return subprocess.CompletedProcess(arguments, 0)

        with patch("scripts.build_site.subprocess.run", side_effect=run) as run_mock:
            self.assertEqual(
                resolver.resolve(
                    ("vane-ai==0.2.0",),
                    requires_python=">=3.10,<3.15",
                    distribution_name="vane-ai",
                    reject_vane=False,
                ),
                ("vane-ai==0.2.0",),
            )
            with self.assertRaisesRegex(SiteBuildError, "Vane-owned"):
                resolver.resolve(
                    ("vane-ai==0.2.0",),
                    requires_python=">=3.10,<3.15",
                    distribution_name="vane-extension-test",
                    reject_vane=True,
                )

        self.assertEqual(run_mock.call_count, 1)

    def test_uv_resolution_failure_is_reported_without_process_output(self) -> None:
        resolver = UvPublicDependencyResolver()
        completed = subprocess.CompletedProcess([], 1)

        with (
            patch("scripts.build_site.subprocess.run", return_value=completed),
            self.assertRaisesRegex(SiteBuildError, "could not resolve"),
        ):
            resolver.resolve(
                ("sdist-only-package==1",),
                requires_python=">=3.10,<3.15",
                distribution_name="vane-extension-test",
                reject_vane=False,
            )

    def test_distinct_uv_resolutions_run_concurrently(self) -> None:
        resolver = UvPublicDependencyResolver()
        subprocess_barrier = Barrier(2)

        def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            subprocess_barrier.wait(timeout=5)
            output_path = Path(arguments[arguments.index("--output-file") + 1])
            output_path.write_bytes(b"resolved-package==1.0\n")
            return subprocess.CompletedProcess(arguments, 0)

        def resolve(requirement: str) -> tuple[str, ...]:
            return resolver.resolve(
                (requirement,),
                requires_python=">=3.10,<3.15",
                distribution_name=requirement.split("=", 1)[0],
                reject_vane=False,
            )

        with (
            patch("scripts.build_site.subprocess.run", side_effect=run) as run_mock,
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = tuple(
                executor.map(resolve, ("first-package==1", "second-package==1"))
            )

        self.assertEqual(
            results,
            (("resolved-package==1.0",), ("resolved-package==1.0",)),
        )
        self.assertEqual(run_mock.call_count, 2)


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
                    "upload_time_iso_8601": (
                        f"2026-09-02T11:{index:02d}:00Z"
                    ),
                    **(
                        {
                            "url": "https://files.example/artifact.whl",
                            "digests": {
                                "sha256": "not-published-by-the-registry"
                            },
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


def _release_response(
    distribution_name: str,
    version: str,
    requires_dist: list[str],
    *,
    requires_python: str | None = ">=3.10,<3.15",
    wheel_tags: tuple[str, ...] | None = None,
    file_requires_python: tuple[str | None, ...] | None = None,
) -> dict[str, object]:
    return _package_response(
        distribution_name,
        version,
        requires_dist=requires_dist,
        requires_python=requires_python,
        wheel_tags=wheel_tags,
        file_requires_python=file_requires_python,
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


def _public_release_responses(*pins: str) -> dict[str, object]:
    result = {}
    for pin in pins:
        requirement = Requirement(pin)
        name = canonicalize_name(requirement.name)
        version = next(iter(requirement.specifier)).version
        result[f"https://pypi.org/pypi/{name}/{version}/json"] = _release_response(
            name, version, [], wheel_tags=("py3-none-any",)
        )
    return result


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
    def test_powershell_install_script_restores_environment_and_guards_steps(
        self,
    ) -> None:
        script = _powershell_install_script(
            [
                ["python", "package==1; platform_machine == \"O'Reilly\""],
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
        self.assertIn(
            "'package==1; platform_machine == \"O''Reilly\"'", script
        )
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
        self.assertEqual(
            detail["package"]["python_tags"], ["cp314", "py3", "py310"]
        )
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
        public_resolver = _FakePublicDependencyResolver(
            ("public-transitive==1.2.3", f"{distribution}==0.2.0")
        )
        responses.update(_public_release_responses(*public_resolver.resolved))

        detail = build_details(
            manifest_root=self._single_manifest_root(manifest),
            generated_at=GENERATED_AT,
            client=_FakeMetadataClient(responses),
            public_resolver=public_resolver,
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
            f"public-transitive==1.2.3 {distribution}==0.2.0",
        )
        powershell_script = detail["installation"]["powershell_install_script"]
        self.assertIsInstance(powershell_script, str)
        assert isinstance(powershell_script, str)
        self.assertIn(
            "'--index-url' 'https://pypi.org/simple/' "
            f"'public-transitive==1.2.3' '{distribution}==0.2.0'",
            powershell_script,
        )
        self.assertEqual(
            public_resolver.calls,
            [
                (
                    (f"{distribution}===0.2.0",),
                    ">=3.10,<3.15",
                    distribution,
                    False,
                )
            ],
        )

    def test_optional_download_metrics_do_not_block_a_published_provider(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        manifest["package_index"] = "pypi"
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        slug = repository.removeprefix("https://github.com/")
        public_resolver = _FakePublicDependencyResolver((f"{distribution}==0.2.0",))
        responses = {
            f"https://api.github.com/repos/{slug}": _github_response(repository),
            f"https://pypi.org/pypi/{distribution}/json": _package_response(
                distribution
            ),
            **_public_release_responses(*public_resolver.resolved),
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
                    public_resolver=public_resolver,
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

    def test_pypi_recipe_requires_a_wheel_for_the_provider(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        manifest["package_index"] = "pypi"
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        responses = {
            (
                "https://api.github.com/repos/"
                f"{repository.removeprefix('https://github.com/')}"
            ): _github_response(repository),
            f"https://pypi.org/pypi/{distribution}/json": (
                _sdist_only_response(distribution)
            ),
            f"https://pypistats.org/api/packages/{distribution}/recent": {
                "data": {"last_week": 123}
            },
        }
        public_resolver = _FakePublicDependencyResolver(
            (f"{distribution}==0.2.0",)
        )

        with self.assertRaisesRegex(SiteBuildError, "non-yanked wheel"):
            build_details(
                manifest_root=self._single_manifest_root(manifest),
                generated_at=GENERATED_AT,
                client=_FakeMetadataClient(responses),
                public_resolver=public_resolver,
            )
        self.assertEqual(public_resolver.calls, [])

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
        public_resolver = _FakePublicDependencyResolver(
            (
                "default-extra==1.0",
                "numpy==2.5.2",
                'platform-marker==1.0; sys_platform == "extra"',
                "transitive-public==2.0",
                "typing-extensions==4.16.0",
            )
        )
        responses.update(_public_release_responses(*public_resolver.resolved))

        detail = build_details(
            manifest_root=self._single_manifest_root(manifest),
            generated_at=GENERATED_AT,
            client=_FakeMetadataClient(responses),
            public_resolver=public_resolver,
        )[0]

        posix_script = detail["installation"]["posix_install_script"]
        powershell_script = detail["installation"]["powershell_install_script"]
        self.assertIsInstance(posix_script, str)
        self.assertIsInstance(powershell_script, str)
        assert isinstance(posix_script, str)
        assert isinstance(powershell_script, str)
        public_command, testpypi_command = posix_script.split(" &&\n")
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
        self.assertEqual(
            public_resolver.calls,
            [
                (
                    (
                        "default-extra",
                        "numpy>=2",
                        'platform-marker; sys_platform == "extra"',
                        "typing-extensions",
                    ),
                    ">=3.10,<3.15",
                    distribution,
                    True,
                )
            ],
        )
        self.assertIn("--index-url https://pypi.org/simple/", public_command)
        isolated_pip = (
            "env PIP_CONFIG_FILE=/dev/null python -m pip --isolated install"
        )
        self.assertTrue(public_command.startswith(isolated_pip))
        self.assertIn("numpy==2.5.2", public_command)
        self.assertIn("typing-extensions==4.16.0", public_command)
        self.assertIn("platform-marker==1.0", public_command)
        self.assertIn("default-extra==1.0", public_command)
        self.assertIn("transitive-public==2.0", public_command)
        self.assertNotIn("numpy>=2", public_command)
        self.assertNotIn("test.pypi.org", public_command)
        self.assertNotIn("vane-", public_command)
        self.assertNotIn("optional-sdk", public_command)
        self.assertNotIn("optional-url", public_command)
        self.assertIn("--force-reinstall", public_command)
        self.assertIn("--no-deps", public_command)
        self.assertIn("--only-binary=:all:", public_command)
        self.assertIn("--force-reinstall", testpypi_command)
        self.assertTrue(testpypi_command.startswith(isolated_pip))
        self.assertIn("--no-deps", testpypi_command)
        self.assertIn("--only-binary=:all:", testpypi_command)
        self.assertIn("--index-url https://test.pypi.org/simple/", testpypi_command)
        self.assertIn(f"vane-ai==={vane_version}", testpypi_command)
        self.assertIn(f"vane-extension-avro==={avro_version}", testpypi_command)
        self.assertIn(f"{distribution}==={provider_version}", testpypi_command)
        self.assertNotIn("vane-extension-optional", testpypi_command)
        self.assertNotIn("https://pypi.org/simple/", testpypi_command)
        self.assertTrue(powershell_script.startswith("& {\n"))
        self.assertEqual(powershell_script.count("    & 'python' '-m' 'pip'"), 2)
        self.assertEqual(powershell_script.count("if (-not $__vanePipSucceeded"), 2)
        self.assertIn(
            "'platform-marker==1.0; sys_platform == \"extra\"'",
            powershell_script,
        )
        self.assertIn(
            "'--index-url' 'https://test.pypi.org/simple/'",
            powershell_script,
        )
        self.assertIn(
            f"'vane-ai==={vane_version}'", powershell_script
        )
        self.assertNotIn("/dev/null", powershell_script)
        self.assertIn("} finally {", powershell_script)
        self.assertNotIn(
            "--extra-index-url",
            f"{posix_script}\n{powershell_script}",
        )
        self.assertNotIn("example.invalid", json.dumps(detail))

    def test_testpypi_preserves_and_propagates_internal_conditions(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        older_vane = "0.2.0.dev1"
        newer_vane = "0.2.0.dev2"
        avro_version = "1.0"
        responses = {
            (
                "https://api.github.com/repos/"
                f"{repository.removeprefix('https://github.com/')}"
            ): _github_response(repository),
            f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                distribution,
                requires_dist=[
                    (
                        f"vane-extension-avro==={avro_version}; "
                        'sys_platform == "linux"'
                    ),
                    f'vane-ai==={newer_vane}; python_version >= "3.14"',
                ],
            ),
            (
                "https://test.pypi.org/pypi/"
                f"vane-extension-avro/{avro_version}/json"
            ): _release_response(
                "vane-extension-avro",
                avro_version,
                [f'vane-ai==={older_vane}; python_version < "3.14"'],
            ),
            f"https://test.pypi.org/pypi/vane-ai/{older_vane}/json": (
                _release_response(
                    "vane-ai",
                    older_vane,
                    ['old-public>=1; platform_machine == "x86_64"'],
                    requires_python=">=3.10,<3.14",
                )
            ),
            f"https://test.pypi.org/pypi/vane-ai/{newer_vane}/json": (
                _release_response(
                    "vane-ai",
                    newer_vane,
                    ["new-public>=2"],
                    requires_python=">=3.14,<3.15",
                )
            ),
        }
        public_resolver = _FakePublicDependencyResolver(
            (
                'new-public==2.1; python_version >= "3.14"',
                'old-public==1.2; python_version < "3.14"',
            )
        )
        responses.update(_public_release_responses(*public_resolver.resolved))

        detail = build_details(
            manifest_root=self._single_manifest_root(manifest),
            generated_at=GENERATED_AT,
            client=_FakeMetadataClient(responses),
            public_resolver=public_resolver,
        )[0]

        public_inputs = {
            requirement.name: requirement
            for requirement in map(Requirement, public_resolver.calls[0][0])
        }
        self.assertEqual(
            str(public_inputs["new-public"].marker),
            'python_version >= "3.14"',
        )
        old_public_marker = str(public_inputs["old-public"].marker)
        self.assertIn('sys_platform == "linux"', old_public_marker)
        self.assertIn('python_version < "3.14"', old_public_marker)
        self.assertIn('platform_machine == "x86_64"', old_public_marker)
        self.assertTrue(public_resolver.calls[0][3])

        posix_script = detail["installation"]["posix_install_script"]
        self.assertIsInstance(posix_script, str)
        assert isinstance(posix_script, str)
        public_command, testpypi_command = posix_script.split(" &&\n")
        public_arguments = shlex.split(public_command)
        installed_public = {
            requirement.name: requirement
            for requirement in map(Requirement, public_arguments[-2:])
        }
        self.assertEqual(
            str(installed_public["new-public"].marker),
            'python_version >= "3.14"',
        )
        self.assertEqual(
            str(installed_public["old-public"].marker),
            'python_version < "3.14"',
        )

        testpypi_arguments = shlex.split(testpypi_command)
        internal_requirements = [
            Requirement(argument)
            for argument in testpypi_arguments
            if canonicalize_name(argument.split("=", 1)[0]).startswith("vane-")
        ]
        by_version = {
            next(iter(requirement.specifier)).version: requirement
            for requirement in internal_requirements
            if canonicalize_name(requirement.name) == "vane-ai"
        }
        self.assertEqual(set(by_version), {older_vane, newer_vane})
        older_marker = str(by_version[older_vane].marker)
        self.assertIn('sys_platform == "linux"', older_marker)
        self.assertIn('python_version < "3.14"', older_marker)
        self.assertEqual(
            str(by_version[newer_vane].marker),
            'python_version >= "3.14"',
        )
        avro = next(
            requirement
            for requirement in internal_requirements
            if canonicalize_name(requirement.name) == "vane-extension-avro"
        )
        self.assertEqual(str(avro.marker), 'sys_platform == "linux"')

    def test_testpypi_rejects_incompatible_internal_requires_python(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        vane_version = "0.2.0.dev1"
        responses = {
            (
                "https://api.github.com/repos/"
                f"{repository.removeprefix('https://github.com/')}"
            ): _github_response(repository),
            f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                distribution,
                requires_dist=[f"vane-ai==={vane_version}"],
                requires_python=">=3.10,<3.12",
            ),
            f"https://test.pypi.org/pypi/vane-ai/{vane_version}/json": (
                _release_response(
                    "vane-ai",
                    vane_version,
                    [],
                    requires_python=">=3.12",
                )
            ),
        }

        with self.assertRaisesRegex(
            SiteBuildError, "vane-ai==0.2.0.dev1 Requires-Python is incompatible"
        ):
            build_details(
                manifest_root=self._single_manifest_root(manifest),
                generated_at=GENERATED_AT,
                client=_FakeMetadataClient(responses),
                public_resolver=_FakePublicDependencyResolver(()),
            )

    def test_testpypi_rejects_overlapping_internal_versions(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        first_version = "0.2.0.dev1"
        second_version = "0.2.0.dev2"
        responses = {
            (
                "https://api.github.com/repos/"
                f"{repository.removeprefix('https://github.com/')}"
            ): _github_response(repository),
            f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                distribution,
                requires_dist=[
                    f'vane-ai==={first_version}; python_version <= "3.14"',
                    f'vane-ai==={second_version}; python_version >= "3.14"',
                ],
            ),
            f"https://test.pypi.org/pypi/vane-ai/{first_version}/json": (
                _release_response("vane-ai", first_version, [])
            ),
            f"https://test.pypi.org/pypi/vane-ai/{second_version}/json": (
                _release_response("vane-ai", second_version, [])
            ),
        }

        with self.assertRaisesRegex(SiteBuildError, "versions conflict"):
            build_details(
                manifest_root=self._single_manifest_root(manifest),
                generated_at=GENERATED_AT,
                client=_FakeMetadataClient(responses),
                public_resolver=_FakePublicDependencyResolver(()),
            )

    def test_inactive_public_requirement_omits_the_public_install_step(self) -> None:
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
                requires_dist=['legacy-package; python_version < "3"'],
            ),
        }
        public_resolver = _FakePublicDependencyResolver(())

        detail = build_details(
            manifest_root=self._single_manifest_root(manifest),
            generated_at=GENERATED_AT,
            client=_FakeMetadataClient(responses),
            public_resolver=public_resolver,
        )[0]

        posix_script = detail["installation"]["posix_install_script"]
        powershell_script = detail["installation"]["powershell_install_script"]
        self.assertIsInstance(posix_script, str)
        self.assertIsInstance(powershell_script, str)
        assert isinstance(posix_script, str)
        assert isinstance(powershell_script, str)
        self.assertNotIn(" &&\n", posix_script)
        self.assertEqual(powershell_script.count("    & 'python' '-m' 'pip'"), 1)
        self.assertEqual(public_resolver.calls, [])

    def test_public_resolution_requires_a_python_range(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        package = _package_response(distribution, requires_dist=["numpy"])
        assert isinstance(package["info"], dict)
        package["info"]["requires_python"] = None
        responses = {
            (
                "https://api.github.com/repos/"
                f"{repository.removeprefix('https://github.com/')}"
            ): _github_response(repository),
            f"https://test.pypi.org/pypi/{distribution}/json": package,
        }
        public_resolver = _FakePublicDependencyResolver(("numpy==2.5.2",))

        with self.assertRaisesRegex(SiteBuildError, "declare Requires-Python"):
            build_details(
                manifest_root=self._single_manifest_root(manifest),
                generated_at=GENERATED_AT,
                client=_FakeMetadataClient(responses),
                public_resolver=public_resolver,
            )
        self.assertEqual(public_resolver.calls, [])

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

    def test_file_requires_python_must_match_each_wheel_tag(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        slug = repository.removeprefix("https://github.com/")
        tag = "cp310-none-manylinux_2_28_x86_64"
        for incompatible in (distribution, "vane-ai", "public-sdk"):
            responses = {
                f"https://api.github.com/repos/{slug}": _github_response(repository),
                f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                    distribution,
                    requires_dist=[]
                    if incompatible == distribution
                    else [f"{incompatible}==1.0"],
                    wheel_tags=(tag,),
                    file_requires_python=(">=3.12",)
                    if incompatible == distribution
                    else None,
                ),
            }
            if incompatible != distribution:
                index = "test.pypi.org" if incompatible == "vane-ai" else "pypi.org"
                responses[f"https://{index}/pypi/{incompatible}/1.0/json"] = (
                    _release_response(
                        incompatible,
                        "1.0",
                        [],
                        wheel_tags=(tag,),
                        file_requires_python=(">=3.12",),
                    )
                )
            with (
                self.subTest(incompatible=incompatible),
                self.assertRaisesRegex(
                    SiteBuildError, "wheel compatible with its Requires-Python"
                ),
            ):
                build_details(
                    manifest_root=self._single_manifest_root(manifest),
                    generated_at=GENERATED_AT,
                    client=_FakeMetadataClient(responses),
                    public_resolver=_FakePublicDependencyResolver(
                        ("public-sdk==1.0",) if incompatible == "public-sdk" else ()
                    ),
                )

    def test_file_requires_python_constrains_the_entire_wheel_closure(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        slug = repository.removeprefix("https://github.com/")
        for package_index, dependency in (
            ("testpypi", "vane-ai"),
            ("testpypi", "public-sdk"),
            ("pypi", "public-sdk"),
        ):
            manifest["package_index"] = package_index
            index = "test.pypi.org" if package_index == "testpypi" else "pypi.org"
            dependency_index = (
                "test.pypi.org" if dependency == "vane-ai" else "pypi.org"
            )
            package = _package_response(
                distribution,
                requires_dist=[f"{dependency}==1.0"],
                wheel_tags=("py3-none-any",),
                file_requires_python=("<3.12",),
            )
            responses = {
                f"https://api.github.com/repos/{slug}": _github_response(repository),
                f"https://{index}/pypi/{distribution}/json": package,
                f"https://{dependency_index}/pypi/{dependency}/1.0/json": _release_response(
                    dependency,
                    "1.0",
                    [],
                    wheel_tags=("py3-none-any",),
                    file_requires_python=(">=3.12",),
                ),
            }
            public_lock = ("public-sdk==1.0",) if dependency == "public-sdk" else ()
            if package_index == "pypi":
                responses[f"https://pypi.org/pypi/{distribution}/0.2.0/json"] = package
                responses[
                    f"https://pypistats.org/api/packages/{distribution}/recent"
                ] = None
                public_lock = (*public_lock, f"{distribution}==0.2.0")
            with (
                self.subTest(package_index=package_index, dependency=dependency),
                self.assertRaises(SiteBuildError),
            ):
                build_details(
                    manifest_root=self._single_manifest_root(manifest),
                    generated_at=GENERATED_AT,
                    client=_FakeMetadataClient(responses),
                    public_resolver=_FakePublicDependencyResolver(public_lock),
                )

    def test_files_sharing_a_tag_keep_alternative_python_environments(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        slug = repository.removeprefix("https://github.com/")
        for file_ranges, minor, succeeds in (
            (("<3.11", ">=3.13"), 10, True),
            (("<3.11", ">=3.13"), 12, False),
            (("<3.11", ">=3.13"), 13, True),
            ((">=3.14,<3.12", None), 12, True),
            (("", None), 12, True),
        ):
            package = _package_response(
                distribution,
                requires_dist=["vane-ai==1.0"],
                wheel_tags=("py3-none-any", "py3-none-any"),
                file_requires_python=file_ranges,
            )
            # A second build of the same release can advertise the same tag.
            package["urls"][1]["filename"] = package["urls"][1]["filename"].replace(
                "-0.2.0-", "-0.2.0-1-"
            )
            responses = {
                f"https://api.github.com/repos/{slug}": _github_response(repository),
                f"https://test.pypi.org/pypi/{distribution}/json": package,
                "https://test.pypi.org/pypi/vane-ai/1.0/json": _release_response(
                    "vane-ai",
                    "1.0",
                    [],
                    wheel_tags=(f"cp3{minor}-none-manylinux_2_28_x86_64",),
                ),
            }
            arguments = {
                "manifest_root": self._single_manifest_root(manifest),
                "generated_at": GENERATED_AT,
                "client": _FakeMetadataClient(responses),
                "public_resolver": _FakePublicDependencyResolver(()),
            }
            with self.subTest(file_ranges=file_ranges, minor=minor):
                if succeeds:
                    detail = build_details(**arguments)[0]
                    self.assertIsNotNone(detail["installation"]["posix_install_script"])
                else:
                    with self.assertRaises(SiteBuildError):
                        build_details(**arguments)

    def test_testpypi_provider_wheel_must_match_requires_python(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        incompatible_tags = {
            "version": "cp39-none-manylinux_2_28_x86_64",
            "invalid-stable-ABI": "cp27-abi3-manylinux_2_28_x86_64",
            "pre-315-free-threaded-stable-ABI": "cp37-abi3t-manylinux_2_28_x86_64",
            "mismatched-interpreter-ABI": "cp310-cp39-manylinux_2_28_x86_64",
            "unsupported-native-ABI": "cp310-unknown-manylinux_2_28_x86_64",
            "premature-free-threaded-ABI": "cp310-cp310t-manylinux_2_28_x86_64",
            "native-ABI-with-universal-platform": "cp310-cp310-any",
            "cpython-ABI-on-pypy": "pp310-cp310-manylinux_2_28_x86_64",
            "mismatched-pypy-ABI": "pp310-pypy39_pp73-manylinux_2_28_x86_64",
            "unknown-pypy-ABI": "pp310-unknown-manylinux_2_28_x86_64",
            "cpython-ABI-on-other-interpreter": "ip310-cp310-win_amd64",
            "unknown-Windows-architecture": "py3-none-win_bogus",
            "unknown-manylinux-architecture": "py3-none-manylinux_2_28_bogus",
            "unknown-musllinux-architecture": "py3-none-musllinux_1_2_bogus",
            "unknown-Linux-architecture": "py3-none-linux_bogus",
            "noncanonical-manylinux-version": "py3-none-manylinux_02_28_x86_64",
            "unsupported-manylinux-baseline": "py3-none-manylinux_2_4_x86_64",
            "unsupported-legacy-manylinux": "py3-none-manylinux1_aarch64",
            "premature-macOS-arm64": "py3-none-macosx_10_9_arm64",
            "invalid-macOS-version": "py3-none-macosx_11_3_x86_64",
        }

        for case, wheel_tag in incompatible_tags.items():
            responses = {
                (
                    "https://api.github.com/repos/"
                    f"{repository.removeprefix('https://github.com/')}"
                ): _github_response(repository),
                f"https://test.pypi.org/pypi/{distribution}/json": (
                    _package_response(
                        distribution,
                        requires_python=">=3.10,<3.15",
                        wheel_tags=(wheel_tag,),
                    )
                ),
            }

            with (
                self.subTest(case=case),
                self.assertRaisesRegex(
                    SiteBuildError,
                    "wheel compatible with its Requires-Python",
                ),
            ):
                build_details(
                    manifest_root=self._single_manifest_root(manifest),
                    generated_at=GENERATED_AT,
                    client=_FakeMetadataClient(responses),
                    public_resolver=_FakePublicDependencyResolver(()),
                )

    def test_linux_platform_model_validates_architectures_and_policies(self) -> None:
        manylinux_architectures = (
            "x86_64",
            "i686",
            "aarch64",
            "armv7l",
            "ppc64",
            "ppc64le",
            "s390x",
            "loongarch64",
            "riscv64",
        )
        supported = [
            (f"{policy}_{architecture}", architecture)
            for policy in ("manylinux_2_28", "musllinux_1_2", "linux")
            for architecture in manylinux_architectures
        ] + [
            ("manylinux1_i686", "i686"),
            ("manylinux2010_x86_64", "x86_64"),
            ("manylinux2014_aarch64", "aarch64"),
            ("manylinux_2_5_x86_64", "x86_64"),
            ("manylinux_2_17_armv7l", "armv7l"),
            ("musllinux_1_0_x86_64", "x86_64"),
            ("linux_i386", "i386"),
            ("linux_armv6l", "armv6l"),
        ]
        for platform_tag, machine in supported:
            with self.subTest(platform_tag=platform_tag):
                self.assertTrue(
                    _wheel_platform_environment(platform_tag).evaluate(
                        {
                            "os_name": "posix",
                            "sys_platform": "linux",
                            "platform_system": "Linux",
                            "platform_machine": machine,
                        }
                    )
                )
        unsupported = [
            f"{policy}_{architecture}"
            for policy in ("manylinux_2_28", "manylinux2014", "musllinux_1_2", "linux")
            for architecture in ("bogus", "amd64", "arm64", "x86_64_extra", "")
        ] + [
            "manylinux1_aarch64",
            "manylinux2010_ppc64le",
            "manylinux2014_loongarch64",
            "manylinux2014_riscv64",
            "manylinux_2_28_i386",
            "manylinux_2_28_armv6l",
            "manylinux_1_99_x86_64",
            "manylinux_2_4_x86_64",
            "manylinux_2_16_aarch64",
            "manylinux_02_28_x86_64",
            "manylinux_2_028_x86_64",
            "musllinux_0_2_x86_64",
            "musllinux_01_2_x86_64",
            "musllinux_1_02_x86_64",
            "manylinux_2_100_x86_64",
            "musllinux_1_100_x86_64",
        ]
        for platform_tag in unsupported:
            with self.subTest(platform_tag=platform_tag):
                self.assertTrue(_wheel_platform_environment(platform_tag).is_empty())

    def test_linux_wheel_overlap_respects_libc_family_and_musl_major(self) -> None:
        for left, right, expected in (
            ("manylinux1_x86_64", "manylinux_2_28_x86_64", True),
            ("manylinux_2_28_x86_64", "musllinux_1_2_x86_64", False),
            ("musllinux_1_1_x86_64", "musllinux_1_2_x86_64", True),
            ("musllinux_1_2_x86_64", "musllinux_2_0_x86_64", False),
            ("linux_x86_64", "musllinux_2_0_x86_64", True),
            ("linux_x86_64", "manylinux_2_28_aarch64", False),
        ):
            with self.subTest(left=left, right=right):
                self.assertEqual(_platform_tags_overlap(left, right), expected)
                self.assertEqual(_platform_tags_overlap(right, left), expected)

    def test_macos_platform_model_requires_a_generated_tag(self) -> None:
        for platform_tag, machines in (
            ("macosx_10_4_x86_64", {"x86_64"}),
            ("macosx_10_16_x86_64", {"x86_64"}),
            ("macosx_10_9_universal2", {"x86_64", "arm64"}),
            ("macosx_10_9_intel", {"x86_64"}),
            ("macosx_11_0_arm64", {"arm64"}),
            ("macosx_26_0_universal2", {"x86_64", "arm64"}),
        ):
            for machine in ("x86_64", "arm64", "bogus"):
                with self.subTest(platform_tag=platform_tag, machine=machine):
                    self.assertEqual(
                        _wheel_platform_environment(platform_tag).evaluate(
                            {
                                "os_name": "posix",
                                "sys_platform": "darwin",
                                "platform_system": "Darwin",
                                "platform_machine": machine,
                            }
                        ),
                        machine in machines,
                    )
        for platform_tag in (
            "macosx_0_0_arm64",
            "macosx_9_9_x86_64",
            "macosx_10_3_x86_64",
            "macosx_10_9_arm64",
            "macosx_10_17_x86_64",
            "macosx_11_3_x86_64",
            "macosx_11_1_arm64",
            "macosx_011_0_arm64",
            "macosx_11_00_arm64",
            "macosx_11_0_bogus",
            "macosx_100_0_arm64",
        ):
            with self.subTest(platform_tag=platform_tag):
                self.assertTrue(_wheel_platform_environment(platform_tag).is_empty())

    def test_windows_platform_model_rejects_unknown_architectures(self) -> None:
        for platform_tag, machine in (
            ("win32", "x86"),
            ("win_amd64", "AMD64"),
            ("win_arm64", "ARM64"),
        ):
            with self.subTest(platform_tag=platform_tag):
                environment = _wheel_platform_environment(platform_tag)
                self.assertTrue(
                    environment.evaluate(
                        {
                            "os_name": "nt",
                            "sys_platform": "win32",
                            "platform_system": "Windows",
                            "platform_machine": machine,
                        }
                    )
                )
        for platform_tag in ("win_bogus", "win_", "win_x86_64", "win_amd64_extra"):
            with self.subTest(platform_tag=platform_tag):
                self.assertTrue(_wheel_platform_environment(platform_tag).is_empty())

    def test_registry_platform_scope_is_explicit_for_non_desktop_wheels(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        package_url = f"https://test.pypi.org/pypi/{distribution}/json"
        for platform_tag in (
            "android_27_arm64_v8a",
            "ios_13_0_arm64_iphoneos",
            "emscripten_3_1_73_wasm32",
            "aix_7105_1841_64",
            "freebsd_13_0_amd64",
        ):
            responses = {
                (
                    "https://api.github.com/repos/"
                    f"{repository.removeprefix('https://github.com/')}"
                ): _github_response(repository),
                package_url: _package_response(
                    distribution,
                    wheel_tags=(f"cp310-none-{platform_tag}",),
                ),
            }
            with self.subTest(platform_tag=platform_tag):
                with self.assertRaisesRegex(
                    SiteBuildError, "supported registry platform"
                ):
                    build_details(
                        manifest_root=self._single_manifest_root(manifest),
                        generated_at=GENERATED_AT,
                        client=_FakeMetadataClient(responses),
                        public_resolver=_FakePublicDependencyResolver(()),
                    )
                responses[package_url] = _package_response(
                    distribution,
                    wheel_tags=(
                        f"cp310-none-{platform_tag}",
                        "cp310-none-manylinux_2_28_x86_64",
                    ),
                )
                detail = build_details(
                    manifest_root=self._single_manifest_root(manifest),
                    generated_at=GENERATED_AT,
                    client=_FakeMetadataClient(responses),
                    public_resolver=_FakePublicDependencyResolver(()),
                )[0]
                self.assertIn(platform_tag, detail["package"]["platform_tags"])
                self.assertIsNotNone(detail["installation"]["posix_install_script"])

    def test_testpypi_rejects_disjoint_internal_wheel_environments(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        vane_version = "0.2.0.dev1"
        provider_tag = "cp313-cp313-manylinux_2_28_x86_64"
        incompatible_tags = {
            "python": "cp314-cp314-manylinux_2_28_x86_64",
            "generic-python": "py314-none-any",
            "ABI": "cp313-cp313t-manylinux_2_28_x86_64",
            "platform": "cp313-cp313-win_amd64",
        }

        for dimension, dependency_tag in incompatible_tags.items():
            responses = {
                (
                    "https://api.github.com/repos/"
                    f"{repository.removeprefix('https://github.com/')}"
                ): _github_response(repository),
                f"https://test.pypi.org/pypi/{distribution}/json": (
                    _package_response(
                        distribution,
                        requires_dist=[f"vane-ai==={vane_version}"],
                        wheel_tags=(provider_tag,),
                    )
                ),
                f"https://test.pypi.org/pypi/vane-ai/{vane_version}/json": (
                    _release_response(
                        "vane-ai",
                        vane_version,
                        [],
                        wheel_tags=(dependency_tag,),
                    )
                ),
            }

            with (
                self.subTest(dimension=dimension),
                self.assertRaisesRegex(
                    SiteBuildError, "has no non-yanked wheel compatible"
                ),
            ):
                build_details(
                    manifest_root=self._single_manifest_root(manifest),
                    generated_at=GENERATED_AT,
                    client=_FakeMetadataClient(responses),
                    public_resolver=_FakePublicDependencyResolver(()),
                )

    def test_testpypi_abi3t_requires_a_matching_python_315_runtime(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        cases = {
            "free-threaded-runtime": ("cp315t", "cp37", True),
            "GIL-runtime": ("cp315", "cp37", False),
            "newer-limited-API": ("cp315t", "cp316", False),
        }
        for case, (runtime_abi, api_target, compatible) in cases.items():
            responses = {
                (
                    "https://api.github.com/repos/"
                    f"{repository.removeprefix('https://github.com/')}"
                ): _github_response(repository),
                f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                    distribution,
                    requires_python=">=3.15,<3.16",
                    requires_dist=["vane-ai===1.0"],
                    wheel_tags=(f"cp315-{runtime_abi}-manylinux_2_28_x86_64",),
                ),
                "https://test.pypi.org/pypi/vane-ai/1.0/json": _release_response(
                    "vane-ai",
                    "1.0",
                    [],
                    requires_python=">=3.15,<3.17",
                    wheel_tags=(f"{api_target}-abi3t-manylinux_2_28_x86_64",),
                ),
            }
            with self.subTest(case=case):
                arguments = {
                    "manifest_root": self._single_manifest_root(manifest),
                    "generated_at": GENERATED_AT,
                    "client": _FakeMetadataClient(responses),
                    "public_resolver": _FakePublicDependencyResolver(()),
                }
                if compatible:
                    detail = build_details(**arguments)[0]
                    self.assertIn(
                        "vane-ai===1.0",
                        detail["installation"]["posix_install_script"],
                    )
                else:
                    with self.assertRaisesRegex(
                        SiteBuildError, "has no non-yanked wheel compatible"
                    ):
                        build_details(**arguments)

    def test_testpypi_accepts_standard_compatible_wheel_tags(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        vane_version = "0.2.0.dev1"
        compatible_tags = {
            "abi-none": (
                "cp310-none-manylinux_2_28_x86_64",
                "cp310-cp310-manylinux_2_28_x86_64",
            ),
            "stable-abi": (
                "cp314-cp314-manylinux_2_28_x86_64",
                "cp310-abi3-manylinux_2_28_x86_64",
            ),
            "universal": (
                "py3-none-any",
                "cp310-cp310-win_amd64",
            ),
            "generic-forward-compatible": (
                "cp314-none-manylinux_2_28_x86_64",
                "py310-none-any",
            ),
            "debug-and-release-ABI": (
                "cp310-cp310d-manylinux_2_28_x86_64",
                "cp310-cp310-manylinux_2_28_x86_64",
            ),
            "pypy-native-ABI": (
                "py3-none-any",
                "pp310-pypy310_pp73-manylinux_2_28_x86_64",
            ),
            "pymalloc-stable-ABI": (
                "cp37-cp37m-manylinux_2_28_x86_64",
                "cp34-abi3-manylinux_2_28_x86_64",
            ),
        }

        for case, (provider_tag, dependency_tag) in compatible_tags.items():
            requires_python = (
                ">=3.4,<3.8" if case == "pymalloc-stable-ABI" else ">=3.10,<3.15"
            )
            responses = {
                (
                    "https://api.github.com/repos/"
                    f"{repository.removeprefix('https://github.com/')}"
                ): _github_response(repository),
                f"https://test.pypi.org/pypi/{distribution}/json": (
                    _package_response(
                        distribution,
                        requires_dist=[f"vane-ai==={vane_version}"],
                        requires_python=requires_python,
                        wheel_tags=(provider_tag,),
                    )
                ),
                f"https://test.pypi.org/pypi/vane-ai/{vane_version}/json": (
                    _release_response(
                        "vane-ai",
                        vane_version,
                        [],
                        requires_python=requires_python,
                        wheel_tags=(dependency_tag,),
                    )
                ),
            }

            with self.subTest(case=case):
                detail = build_details(
                    manifest_root=self._single_manifest_root(manifest),
                    generated_at=GENERATED_AT,
                    client=_FakeMetadataClient(responses),
                    public_resolver=_FakePublicDependencyResolver(()),
                )[0]
                self.assertIsNotNone(
                    detail["installation"]["posix_install_script"]
                )

    def test_cpython_abi_overlap_uses_the_complete_native_abi_form(self) -> None:
        for abi in ("cp37m", "cp32mu"):
            with self.subTest(abi=abi):
                self.assertTrue(_abi_tags_overlap(abi, "abi3"))
                self.assertTrue(_abi_tags_overlap("abi3", abi))
        self.assertFalse(_abi_tags_overlap("cp27mu", "abi3"))
        self.assertFalse(_abi_tags_overlap("cp37m", "cp37dm"))

    def test_public_wheels_share_the_entire_internal_environment(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        linux_tag = "cp310-cp310-manylinux_2_28_x86_64"
        windows_tag = "cp310-cp310-win_amd64"
        universal_tag = "py3-none-any"
        cases = {
            "platform-mismatch": (
                linux_tag,
                universal_tag,
                (windows_tag,),
                None,
                False,
            ),
            "internal-restricts-platform": (
                universal_tag,
                linux_tag,
                (windows_tag,),
                None,
                False,
            ),
            "disjoint-public-siblings": (
                universal_tag,
                universal_tag,
                (linux_tag, windows_tag),
                None,
                False,
            ),
            "inactive-platform-dependency": (
                linux_tag,
                linux_tag,
                (windows_tag,),
                'sys_platform == "win32"',
                True,
            ),
            "compatible-public-wheel": (
                linux_tag,
                linux_tag,
                (universal_tag,),
                None,
                True,
            ),
            "python-mismatch": (
                linux_tag,
                universal_tag,
                ("cp39-none-manylinux_2_28_x86_64",),
                None,
                False,
            ),
            "ABI-mismatch": (
                "cp313-cp313-manylinux_2_28_x86_64",
                universal_tag,
                ("cp313-cp313t-manylinux_2_28_x86_64",),
                None,
                False,
            ),
        }
        for case, (
            provider_tag,
            internal_tag,
            public_tags,
            condition,
            compatible,
        ) in cases.items():
            pins = tuple(
                f"public-sdk-{index}==1.0" + (f"; {condition}" if condition else "")
                for index in range(len(public_tags))
            )
            responses = {
                (
                    "https://api.github.com/repos/"
                    f"{repository.removeprefix('https://github.com/')}"
                ): _github_response(repository),
                f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                    distribution,
                    requires_dist=["vane-ai===1.0", *pins],
                    wheel_tags=(provider_tag,),
                ),
                "https://test.pypi.org/pypi/vane-ai/1.0/json": _release_response(
                    "vane-ai",
                    "1.0",
                    [],
                    wheel_tags=(internal_tag,),
                ),
                **{
                    f"https://pypi.org/pypi/public-sdk-{index}/1.0/json": _release_response(
                        f"public-sdk-{index}",
                        "1.0",
                        [],
                        requires_python=">=3.9,<3.15",
                        wheel_tags=(tag,),
                    )
                    for index, tag in enumerate(public_tags)
                },
            }
            with self.subTest(case=case):
                arguments = {
                    "manifest_root": self._single_manifest_root(manifest),
                    "generated_at": GENERATED_AT,
                    "client": _FakeMetadataClient(responses),
                    "public_resolver": _FakePublicDependencyResolver(pins),
                }
                if compatible:
                    detail = build_details(**arguments)[0]
                    script = detail["installation"]["posix_install_script"]
                    if case == "inactive-platform-dependency":
                        self.assertNotIn("public-sdk", script)
                        self.assertEqual(arguments["public_resolver"].calls, [])
                    else:
                        public_step, internal_step = script.split(" &&\n")
                        self.assertIn("public-sdk-0==1.0", public_step)
                        self.assertNotIn("public-sdk", internal_step)
                else:
                    with self.assertRaisesRegex(
                        SiteBuildError, "no common environment"
                    ):
                        build_details(**arguments)

    def test_testpypi_requires_a_common_environment_for_all_internal_wheels(
        self,
    ) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        cases = {
            "disjoint-siblings": ((10,), (11,)),
            "pairwise-but-not-collectively-compatible": (
                (10, 11), (11, 12), (10, 12),
            ),
            "common-environment": ((10, 11), (11, 12), (11,)),
        }
        for case, minor_versions in cases.items():
            dependencies = {
                f"vane-extension-dependency-{index}": versions
                for index, versions in enumerate(minor_versions)
            }
            responses = {
                (
                    "https://api.github.com/repos/"
                    f"{repository.removeprefix('https://github.com/')}"
                ): _github_response(repository),
                f"https://test.pypi.org/pypi/{distribution}/json": (
                    _package_response(
                        distribution,
                        requires_dist=[f"{name}===1.0" for name in dependencies],
                        wheel_tags=("py3-none-any",),
                    )
                ),
                **{
                    f"https://test.pypi.org/pypi/{name}/1.0/json": (
                        _release_response(
                            name,
                            "1.0",
                            [],
                            wheel_tags=tuple(
                                f"cp3{minor}-none-manylinux_2_28_x86_64"
                                for minor in versions
                            ),
                        )
                    )
                    for name, versions in dependencies.items()
                },
            }
            arguments = {
                "manifest_root": self._single_manifest_root(manifest),
                "generated_at": GENERATED_AT,
                "client": _FakeMetadataClient(responses),
                "public_resolver": _FakePublicDependencyResolver(()),
            }
            with self.subTest(case=case):
                if case == "common-environment":
                    detail = build_details(**arguments)[0]
                    self.assertIsNotNone(
                        detail["installation"]["posix_install_script"]
                    )
                else:
                    with self.assertRaisesRegex(
                        SiteBuildError, "wheel closure has no common environment"
                    ):
                        build_details(**arguments)

    def test_unreachable_provider_dependencies_are_not_fetched_or_resolved(
        self,
    ) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        slug = repository.removeprefix("https://github.com/")
        linux = "manylinux_2_28_x86_64"
        for condition, tag, file_python in (
            ('sys_platform == "win32"', f"cp310-none-{linux}", None),
            ('platform_machine == "aarch64"', f"cp310-none-{linux}", None),
            ('implementation_name == "pypy"', f"cp310-none-{linux}", None),
            ('python_version >= "3.12"', f"py3-none-{linux}", "<3.11"),
        ):
            responses = {
                f"https://api.github.com/repos/{slug}": _github_response(repository),
                f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                    distribution,
                    requires_dist=[
                        f"vane-ai===1; {condition}",
                        f"public-sdk>=1; {condition}",
                    ],
                    wheel_tags=(tag,),
                    file_requires_python=(file_python,),
                ),
            }
            client = _FakeMetadataClient(responses)
            resolver = _FakePublicDependencyResolver(())
            with self.subTest(condition=condition):
                detail = build_details(
                    manifest_root=self._single_manifest_root(manifest),
                    generated_at=GENERATED_AT,
                    client=client,
                    public_resolver=resolver,
                )[0]
                self.assertEqual(resolver.calls, [])
                self.assertEqual({url for url, *_ in client.requests}, set(responses))
                script = detail["installation"]["posix_install_script"]
                self.assertIsNotNone(script)
                self.assertNotIn("vane-ai", script)
                self.assertNotIn("public-sdk", script)

    def test_internal_python_checks_use_actual_provider_environments(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        slug = repository.removeprefix("https://github.com/")
        for child_python, minor, succeeds in (
            (">=3.10,<3.11", 10, True),
            (">=3.11,<3.15", 14, False),
        ):
            responses = {
                f"https://api.github.com/repos/{slug}": _github_response(repository),
                f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                    distribution,
                    requires_dist=["vane-ai===1"],
                    wheel_tags=("cp310-none-manylinux_2_28_x86_64",),
                ),
                "https://test.pypi.org/pypi/vane-ai/1/json": _release_response(
                    "vane-ai",
                    "1",
                    [],
                    requires_python=child_python,
                    wheel_tags=(f"cp3{minor}-none-manylinux_2_28_x86_64",),
                ),
            }
            arguments = {
                "manifest_root": self._single_manifest_root(manifest),
                "generated_at": GENERATED_AT,
                "client": _FakeMetadataClient(responses),
                "public_resolver": _FakePublicDependencyResolver(()),
            }
            with self.subTest(child_python=child_python):
                if succeeds:
                    self.assertIsNotNone(
                        build_details(**arguments)[0]["installation"][
                            "posix_install_script"
                        ]
                    )
                else:
                    with self.assertRaisesRegex(
                        SiteBuildError, "Requires-Python is incompatible"
                    ):
                        build_details(**arguments)

    def test_internal_version_conflicts_use_actual_provider_environments(self) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        slug = repository.removeprefix("https://github.com/")
        for minors, succeeds in (((10, 14), True), ((10, 12, 14), False)):
            responses = {
                f"https://api.github.com/repos/{slug}": _github_response(repository),
                f"https://test.pypi.org/pypi/{distribution}/json": _package_response(
                    distribution,
                    requires_dist=[
                        'vane-ai===1; python_version < "3.14"',
                        'vane-ai===2; python_version >= "3.11"',
                    ],
                    wheel_tags=tuple(
                        f"cp3{minor}-none-manylinux_2_28_x86_64" for minor in minors
                    ),
                ),
                **{
                    f"https://test.pypi.org/pypi/vane-ai/{version}/json": _release_response(
                        "vane-ai",
                        version,
                        [],
                        requires_python=child_python,
                        wheel_tags=("py3-none-manylinux_2_28_x86_64",),
                    )
                    for version, child_python in (
                        ("1", ">=3.10,<3.14"),
                        ("2", ">=3.11,<3.15"),
                    )
                },
            }
            arguments = {
                "manifest_root": self._single_manifest_root(manifest),
                "generated_at": GENERATED_AT,
                "client": _FakeMetadataClient(responses),
                "public_resolver": _FakePublicDependencyResolver(()),
            }
            with self.subTest(minors=minors):
                if succeeds:
                    self.assertIsNotNone(
                        build_details(**arguments)[0]["installation"][
                            "posix_install_script"
                        ]
                    )
                else:
                    with self.assertRaisesRegex(
                        SiteBuildError, "dependency versions conflict"
                    ):
                        build_details(**arguments)

    def test_testpypi_ignores_wheel_mismatch_outside_provider_platforms(
        self,
    ) -> None:
        manifest = dict(_checked_in_manifest("iceberg"))
        distribution = str(manifest["distribution_name"])
        repository = str(manifest["repository"])
        windows_version = "0.2.0.dev1"
        linux_version = "0.2.0"
        responses = {
            (
                "https://api.github.com/repos/"
                f"{repository.removeprefix('https://github.com/')}"
            ): _github_response(repository),
            f"https://test.pypi.org/pypi/{distribution}/json": (
                _package_response(
                    distribution,
                    requires_dist=[
                        (
                            f"vane-ai==={windows_version}; "
                            'sys_platform == "win32"'
                        ),
                        f"vane-extension-avro==={linux_version}",
                    ],
                    wheel_tags=(
                        "cp310-none-manylinux_2_28_x86_64",
                    ),
                )
            ),
            f"https://test.pypi.org/pypi/vane-ai/{windows_version}/json": (
                _release_response(
                    "vane-ai",
                    windows_version,
                    [],
                    wheel_tags=("cp310-cp310-win_amd64",),
                )
            ),
            (
                "https://test.pypi.org/pypi/"
                f"vane-extension-avro/{linux_version}/json"
            ): _release_response(
                "vane-extension-avro",
                linux_version,
                [],
                wheel_tags=(
                    "cp310-none-manylinux_2_28_x86_64",
                ),
            ),
        }

        detail = build_details(
            manifest_root=self._single_manifest_root(manifest),
            generated_at=GENERATED_AT,
            client=_FakeMetadataClient(responses),
            public_resolver=_FakePublicDependencyResolver(()),
        )[0]

        self.assertIsNotNone(detail["installation"]["posix_install_script"])

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
