from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_catalog import (
    CATALOG_MAX_BYTES,
    PROJECT_ROOT,
    CatalogBuildError,
    build_catalog,
    catalog_bytes,
)


class BuildCatalogTests(unittest.TestCase):
    def _manifest(self, root: Path, name: str = "sample", **updates: object) -> Path:
        value: dict[str, object] = {
            "$schema": "../../schema/extension.schema.json",
            "extension_name": name,
            "distribution_name": f"vane-extension-{name.replace('_', '-')}",
            "description": "A sample extension.",
            "repository": "https://github.com/AstroVela/sample",
            "publisher": "AstroVela",
            "license": "Apache-2.0",
            "maintainers": ["AstroVela"],
            "package_index": "testpypi",
            "documentation": {
                "url": "https://github.com/AstroVela/sample/tree/main/docs",
                "hello_world": "SELECT sample();",
                "extended_description": "A longer sample description.",
            },
        }
        value.update(updates)
        directory = root / name
        directory.mkdir()
        path = directory / "extension.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_checked_in_catalog_is_current(self) -> None:
        self.assertEqual((PROJECT_ROOT / "index.json").read_bytes(), catalog_bytes())

    def test_registry_must_contain_at_least_one_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)

            with self.assertRaisesRegex(CatalogBuildError, "at least one"):
                build_catalog(root)

    def test_catalog_is_sorted_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._manifest(root, "zeta")
            self._manifest(root, "alpha")

            self.assertEqual(
                ["alpha", "zeta"],
                [
                    entry["extension_name"]
                    for entry in build_catalog(root)["extensions"]
                ],
            )

    def test_catalog_contract_excludes_detail_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._manifest(root)

            entry = build_catalog(root)["extensions"][0]

            self.assertNotIn("maintainers", entry)
            self.assertNotIn("package_index", entry)
            self.assertNotIn("documentation", entry)

    def test_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = self._manifest(root)
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    '"extension_name": "sample",',
                    '"extension_name": "sample", "extension_name": "sample",',
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CatalogBuildError, "repeats key"):
                build_catalog(root)

    def test_manifest_must_be_utf8_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = self._manifest(root)
            path.write_bytes(b"\xff")

            with self.assertRaisesRegex(CatalogBuildError, "UTF-8 JSON"):
                build_catalog(root)

    def test_manifest_text_must_not_contain_a_lone_unicode_surrogate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._manifest(root, description="\ud800")

            with self.assertRaisesRegex(CatalogBuildError, "lone Unicode surrogates"):
                build_catalog(root)

    def test_extension_name_must_map_to_one_distribution_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._manifest(root, "sample__nested")

            with self.assertRaisesRegex(CatalogBuildError, "extension-name syntax"):
                build_catalog(root)

    def test_distribution_must_match_extension_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._manifest(root, distribution_name="vane-extension-different")

            with self.assertRaisesRegex(CatalogBuildError, "distribution_name must be"):
                build_catalog(root)

    def test_repository_must_not_include_a_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._manifest(root, repository="https://github.com:443/AstroVela/sample")

            with self.assertRaisesRegex(CatalogBuildError, "canonical HTTPS"):
                build_catalog(root)

    def test_package_index_must_be_explicitly_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._manifest(root, package_index="private")

            with self.assertRaisesRegex(CatalogBuildError, "package_index"):
                build_catalog(root)

    def test_maintainers_are_unique_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._manifest(root, maintainers=["AstroVela", "astrovela"])

            with self.assertRaisesRegex(CatalogBuildError, "duplicates"):
                build_catalog(root)

    def test_documentation_allows_newlines_but_rejects_other_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._manifest(
                root,
                documentation={
                    "url": "https://github.com/AstroVela/sample/tree/main/docs",
                    "hello_world": "SELECT 1;\nSELECT 2;",
                    "extended_description": "Invalid\tdescription",
                },
            )

            with self.assertRaisesRegex(CatalogBuildError, "control characters"):
                build_catalog(root)

    def test_documentation_url_can_target_a_page_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._manifest(
                root,
                documentation={
                    "url": "https://example.com/docs?version=1#quick-start",
                    "hello_world": "SELECT sample();",
                    "extended_description": "A longer sample description.",
                },
            )

            self.assertEqual(
                build_catalog(root)["extensions"][0]["extension_name"], "sample"
            )

    def test_generated_catalog_must_fit_the_runtime_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for index in range(128):
                self._manifest(
                    root,
                    f"sample_{index}",
                    description="x" * 500,
                )

            with self.assertRaisesRegex(CatalogBuildError, str(CATALOG_MAX_BYTES)):
                catalog_bytes(root)


if __name__ == "__main__":
    unittest.main()
