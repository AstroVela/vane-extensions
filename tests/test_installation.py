from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Thread
from unittest.mock import patch

from packaging.requirements import Requirement

from scripts.installation import (
    INDEX_URLS,
    PYTHON_TARGETS,
    ResolutionError,
    PdmInstallationResolver,
    _parse_export,
)


def _wheel(
    index: Path, name: str, version: str, requirements: tuple[str, ...] = ()
) -> None:
    directory = index / name
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{name.replace('-', '_')}-{version}"
    filename = f"{stem}-py3-none-any.whl"
    with zipfile.ZipFile(directory / filename, "w") as wheel:
        wheel.writestr(
            f"{stem}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
            "Requires-Python: >=3.10\n"
            + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements)
            + "\n",
        )
        wheel.writestr(
            f"{stem}.dist-info/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n"
        )
        wheel.writestr(f"{stem}.dist-info/RECORD", "")
    (directory / "index.html").write_text(
        "\n".join(
            f'<a href="{file.name}">{file.name}</a>'
            for file in sorted(directory.glob("*.whl"))
        ),
        encoding="utf-8",
    )


def _arguments(**updates: object) -> dict[str, object]:
    return {
        "distribution_name": "vane-extension-sample",
        "version": "1",
        "requires_python": ">=3.10,<3.15",
        "package_index": "testpypi",
        **updates,
    }


@contextmanager
def _indexes(root: Path):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

    with ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(root))
    ) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch.dict(
                INDEX_URLS, {"pypi": f"{base}/pypi/", "testpypi": f"{base}/testpypi/"}
            ):
                yield base
        finally:
            server.shutdown()
            thread.join()


class InstallationTests(unittest.TestCase):
    def test_pdm_resolves_transitive_sources_markers_and_extras_without_a_graph_walker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public, internal = root / "pypi", root / "testpypi"
            _wheel(
                internal,
                "vane-extension-sample",
                "1",
                (
                    "vane-extension-avro==1",
                    'public-wheel==1; python_version < "3.12"',
                    'public-wheel==2; python_version >= "3.12"',
                    'not-selected; extra == "docs"',
                ),
            )
            _wheel(internal, "vane-extension-avro", "1", ("vane-ai==1",))
            _wheel(internal, "vane-ai", "1")
            _wheel(public, "public-wheel", "1")
            _wheel(public, "public-wheel", "2")
            # Both indexes have counterfeit packages. PDM source filters must bind
            # transitive Vane names and keep public dependencies out of TestPyPI.
            _wheel(public, "vane-ai", "1", ("wrong-source-must-not-be-used",))
            _wheel(
                public, "vane-extension-avro", "1", ("wrong-source-must-not-be-used",)
            )
            _wheel(internal, "public-wheel", "2", ("wrong-source-must-not-be-used",))
            with _indexes(root):
                resolved = PdmInstallationResolver().resolve(**_arguments())
            self.assertEqual(
                {Requirement(pin).name for pin in resolved["testpypi"]},
                {"vane-ai", "vane-extension-avro", "vane-extension-sample"},
            )
            self.assertIn("vane-extension-sample==1", resolved["testpypi"])
            self.assertEqual(len(resolved["pypi"]), 2)
            self.assertTrue(
                any(pin.startswith("public-wheel==1;") for pin in resolved["pypi"])
            )
            self.assertTrue(
                any(pin.startswith("public-wheel==2;") for pin in resolved["pypi"])
            )
            for minor in PYTHON_TARGETS:
                pins = [Requirement(pin) for pin in resolved["pypi"]]
                active = [
                    pin
                    for pin in pins
                    if pin.marker.evaluate(
                        {
                            "python_version": minor,
                            "python_full_version": f"{minor}.0",
                            "sys_platform": "linux",
                            "platform_machine": "x86_64",
                            "implementation_name": "cpython",
                            "platform_python_implementation": "CPython",
                        }
                    )
                ]
                self.assertEqual(len(active), 1)
                expected = "1" if minor in ("3.10", "3.11") else "2"
                self.assertTrue(active[0].specifier.contains(expected))
            self.assertNotIn("not-selected", str(resolved))

    def test_source_filters_do_not_add_unused_vane_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public, internal = root / "pypi", root / "testpypi"
            public.mkdir()
            _wheel(internal, "vane-extension-sample", "1")
            with _indexes(root):
                resolved = PdmInstallationResolver().resolve(**_arguments())
            self.assertEqual(
                resolved, {"pypi": (), "testpypi": ("vane-extension-sample==1",)}
            )

    def test_missing_internal_package_does_not_fall_back_to_pypi(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public, internal = root / "pypi", root / "testpypi"
            _wheel(internal, "vane-extension-sample", "1", ("vane-ai==1",))
            _wheel(public, "vane-ai", "1")
            with (
                _indexes(root),
                self.assertRaises(ResolutionError),
            ):
                PdmInstallationResolver().resolve(**_arguments())

    def test_pdm_handles_conflicting_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public, internal = root / "pypi", root / "testpypi"
            _wheel(
                internal,
                "vane-extension-sample",
                "1",
                ("vane-ai==1", "public-wheel==1"),
            )
            _wheel(internal, "vane-ai", "1", ("public-wheel==2",))
            _wheel(public, "public-wheel", "1")
            _wheel(public, "public-wheel", "2")
            with (
                _indexes(root),
                self.assertRaises(ResolutionError),
            ):
                PdmInstallationResolver().resolve(**_arguments())

    def test_export_preserves_pdm_markers_without_platform_inference(self) -> None:
        arguments = _arguments()
        arguments.pop("requires_python")
        exported = (
            b'vane-extension-sample==1\npublic-wheel==2; platform_machine == "AMD64"\n'
        )
        result = _parse_export(exported, **arguments)
        self.assertEqual(
            result["pypi"], ('public-wheel==2; platform_machine == "AMD64"',)
        )

    def test_export_rejects_urls_options_and_unpinned_roots(self) -> None:
        arguments = _arguments()
        arguments.pop("requires_python")
        for exported in (
            b"vane-extension-sample==1\npublic-wheel @ https://user:secret@example.com/x.whl",
            b"vane-extension-sample==1\n--extra-index-url https://example.com/simple",
            b"vane-extension-sample==1\npublic-wheel>=2",
            b"vane-extension-sample==1\npublic-wheel==2.*",
            b"vane-extension-sample==1\npublic-wheel[extra]==2",
            b"public-wheel==2",
            b"\xff",
        ):
            with self.subTest(exported=exported), self.assertRaises(ResolutionError):
                _parse_export(exported, **arguments)

    def test_export_strips_only_the_exact_configured_source_directives(self) -> None:
        arguments = _arguments()
        arguments.pop("requires_python")
        export = (
            "# generated by PDM\nvane-extension-sample==1\n"
            f"--index-url {INDEX_URLS['pypi']}\n"
            f"--extra-index-url {INDEX_URLS['testpypi']}\n"
        ).encode()
        self.assertEqual(
            _parse_export(export, **arguments)["testpypi"],
            ("vane-extension-sample==1",),
        )
        with self.assertRaises(ResolutionError):
            _parse_export(
                export.replace(b"https://test.pypi.org/", b"https://evil.example/"),
                **arguments,
            )

    def test_export_has_a_size_limit(self) -> None:
        arguments = _arguments()
        arguments.pop("requires_python")
        with (
            patch("scripts.installation.EXPORT_MAX_BYTES", 1),
            self.assertRaisesRegex(ResolutionError, "size limit"),
        ):
            _parse_export(b"vane-extension-sample==1", **arguments)

    def test_transitive_urls_are_rejected_before_their_metadata_is_prepared(
        self,
    ) -> None:
        from pdm.exceptions import PdmException
        from pdm.models.candidates import Candidate
        from pdm.models.requirements import parse_line
        from scripts.pdm_runner import RegistryRepository

        repository = object.__new__(RegistryRepository)
        candidate = Candidate(
            parse_line("vane-extension-sample==1"),
            name="vane-extension-sample",
            version="1",
        )
        metadata = (
            [parse_line("bad @ https://user:secret@example.invalid/bad.tar.gz")],
            None,
            "",
        )
        with patch(
            "pdm.models.repositories.PyPIRepository.get_dependencies",
            return_value=metadata,
        ):
            with self.assertRaises(PdmException) as raised:
                repository.get_dependencies(candidate)
            self.assertNotIn("secret", str(raised.exception))
        with patch(
            "pdm.models.repositories.PyPIRepository.get_dependencies"
        ) as prepare:
            with self.assertRaises(PdmException):
                repository.get_dependencies(Candidate(metadata[0][0]))
            prepare.assert_not_called()

    def test_invocation_is_isolated_bounded_and_never_builds(self) -> None:
        def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            root = kwargs["cwd"]
            project = tomllib.loads((root / "pyproject.toml").read_text())
            self.assertEqual(project["project"]["requires-python"], ">=3.10,<3.15")
            self.assertEqual(
                project["project"]["dependencies"], ["vane-extension-sample===1"]
            )
            sources = project["tool"]["pdm"]["source"]
            self.assertEqual(
                sources[0]["exclude_packages"], ["vane-ai", "vane-extension-*"]
            )
            self.assertEqual(
                sources[1]["include_packages"], ["vane-ai", "vane-extension-*"]
            )
            self.assertEqual(kwargs["env"]["PDM_ONLY_BINARY"], ":all:")
            self.assertEqual(kwargs["env"]["PDM_USE_UV"], "false")
            self.assertEqual(kwargs["env"]["PDM_CACHE_DIR"], str(root / "cache"))
            self.assertIn("-I", arguments)
            self.assertIn("--config", arguments)
            self.assertFalse(
                any(key.startswith(("PIP_", "UV_")) for key in kwargs["env"])
            )
            self.assertEqual(kwargs["timeout"], 120)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            if "export" in arguments:
                for option in ("--no-hashes", "--no-extras"):
                    self.assertIn(option, arguments)
                (root / "requirements.txt").write_text("vane-extension-sample==1\n")
            else:
                if "--append" in arguments:
                    self.assertNotIn("--strategy", arguments)
                else:
                    self.assertIn("inherit_metadata", arguments)
                self.assertIn(":all", arguments)
            return subprocess.CompletedProcess(arguments, 0)

        with (
            patch.dict(
                os.environ,
                {"PIP_EXTRA_INDEX_URL": "https://bad.example", "UV_INDEX": "bad"},
            ),
            patch("scripts.installation.subprocess.run", side_effect=run) as execute,
        ):
            resolver = PdmInstallationResolver()
            resolver.resolve(**_arguments())
            resolver.resolve(**_arguments())
            self.assertEqual(execute.call_count, len(PYTHON_TARGETS) + 1)
            locks = execute.call_args_list[:-1]
            self.assertNotIn("--append", locks[0].args[0])
            for call, python in zip(locks, PYTHON_TARGETS):
                self.assertIn(f"=={python}.*", call.args[0])
            for call in locks[1:]:
                self.assertIn("--append", call.args[0])

    def test_distinct_resolutions_run_concurrently(self) -> None:
        barrier = Barrier(2)

        def resolve(**arguments: object) -> dict[str, tuple[str, ...]]:
            barrier.wait(timeout=5)
            return {"pypi": (), "testpypi": (f"{arguments['distribution_name']}==1",)}

        resolver = PdmInstallationResolver()
        with (
            patch.object(resolver, "_resolve_uncached", side_effect=resolve),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(
                pool.map(
                    lambda name: resolver.resolve(**_arguments(distribution_name=name)),
                    ("vane-extension-one", "vane-extension-two"),
                )
            )
        self.assertNotEqual(results[0], results[1])

    def test_process_failure_does_not_expose_output_and_can_be_retried(self) -> None:
        for outcome in (
            subprocess.TimeoutExpired("pdm", 120, stderr="secret"),
            subprocess.CompletedProcess([], 1, stderr="secret"),
        ):
            with patch("scripts.installation.subprocess.run") as execute:
                if isinstance(outcome, Exception):
                    execute.side_effect = outcome
                else:
                    execute.return_value = outcome
                resolver = PdmInstallationResolver()
                for _ in range(2):
                    with self.assertRaises(ResolutionError) as raised:
                        resolver.resolve(**_arguments())
                    self.assertNotIn("secret", str(raised.exception))
                self.assertEqual(execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()
