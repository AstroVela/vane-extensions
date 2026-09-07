from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.smoke_install import smoke_install


class SmokeInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        platform = patch(
            "scripts.smoke_install.platform.platform", return_value="test-runner"
        )
        platform.start()
        self.addCleanup(platform.stop)

    def _site(self, entries: list[dict[str, object]]) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "v1/extensions").mkdir(parents=True)
        (root / "v1/extensions/index.json").write_text(
            json.dumps({"extensions": entries})
        )
        return root

    def _entry(self, name: str, *, published: bool = True) -> dict[str, object]:
        return {
            "extension_name": name,
            "package": {"published": published, "latest_version": "1"},
            "installation": {
                "posix_install_script": "install-command && pip-check-command"
            },
        }

    def test_each_published_provider_uses_a_fresh_environment_and_the_exact_script(
        self,
    ) -> None:
        site = self._site(
            [
                self._entry("one"),
                self._entry("two"),
                self._entry("missing", published=False),
            ]
        )
        with (
            patch("scripts.smoke_install.venv.EnvBuilder") as builder,
            patch("scripts.smoke_install.subprocess.run") as run,
        ):
            self.assertEqual(smoke_install(site), 2)
        environments = [
            call.args[0] for call in builder.return_value.create.call_args_list
        ]
        self.assertEqual(len(set(environments)), 2)
        for index, name in enumerate(("one", "two")):
            install, load = run.call_args_list[index * 2 : index * 2 + 2]
            self.assertEqual(
                install.args[0],
                ["bash", "-e", "-c", "install-command && pip-check-command"],
            )
            self.assertTrue(install.kwargs["check"])
            self.assertEqual(
                load.args[0][:3], [str(environments[index] / "bin/python"), "-I", "-c"]
            )
            self.assertEqual(load.args[0][-1], name)
            self.assertIn('"autoinstall_known_extensions": "false"', load.args[0][-2])

    def test_failed_install_stops_before_load(self) -> None:
        site = self._site([self._entry("one")])
        with (
            patch("scripts.smoke_install.venv.EnvBuilder"),
            patch(
                "scripts.smoke_install.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "pip"),
            ) as run,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            smoke_install(site)
        self.assertEqual(run.call_count, 1)

    def test_a_published_provider_cannot_skip_installation(self) -> None:
        entry = self._entry("one")
        entry["installation"]["posix_install_script"] = None
        with self.assertRaisesRegex(ValueError, "no installation recipe"):
            smoke_install(self._site([entry]))


if __name__ == "__main__":
    unittest.main()
