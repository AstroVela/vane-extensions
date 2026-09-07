"""Run PDM with a registry-only repository policy and no ambient plugins.

PDM's documented repository_class extension point lets us reject non-index
requirements before preparing them. Resolution, markers, and export stay in PDM.
"""

import sys

from pdm.core import Core
from pdm.exceptions import PdmException
from pdm.models.candidates import Candidate
from pdm.models.repositories import PyPIRepository


class RegistryRepository(PyPIRepository):
    def get_dependencies(self, candidate: Candidate):
        if not candidate.req.is_named:
            raise PdmException("Registry recipes accept only named index dependencies")
        result = super().get_dependencies(candidate)
        if any(not requirement.is_named for requirement in result[0]):
            raise PdmException("Registry recipes accept only named index dependencies")
        return result


class RegistryCore(Core):
    repository_class = RegistryRepository

    def load_plugins(self) -> None:
        """Only the checked-in repository policy may customize this invocation."""


if __name__ == "__main__":
    core = RegistryCore()
    with core.exit_stack:
        core.main(sys.argv[1:])
