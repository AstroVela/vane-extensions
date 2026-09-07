# Vane extensions registry

This repository is the public discovery registry for independently published
[Vane](https://github.com/AstroVela/vane) dynamic extension provider packages.
It plays the same narrow role as an extension directory: it tells clients which
provider package owns an extension name and where that provider is maintained.

The machine-readable catalog is published by GitHub Pages at:

```text
https://astrovela.github.io/vane-extensions/v1/index.json
```

That endpoint is the small, stable discovery contract consumed by Vane. Rich
metadata is published separately so documentation and operational fields can
evolve without requiring a Vane release:

```text
https://astrovela.github.io/vane-extensions/v1/extensions/index.json
https://astrovela.github.io/vane-extensions/v1/extensions/<name>.json
https://astrovela.github.io/vane-extensions/v1/metrics/downloads-last-week.json
https://astrovela.github.io/vane-extensions/extensions/<name>/
```

The registry is discovery metadata only. It does not install packages, select
versions, distribute native artifacts, or grant trust to an artifact. Python
package indexes resolve and install provider wheels; Vane validates each
provider's embedded metadata and native descriptor when it is explicitly
loaded.

## Add an extension

1. Publish a Python distribution named `vane-extension-<name>` that exposes a
   `vane.dynamic_extension_providers` entry point named `<name>`. Names use
   lowercase ASCII letters and digits with single, non-trailing underscores so
   every extension maps to exactly one normalized Python distribution name.
2. Add `extensions/<name>/extension.json`, following
   `schema/extension.schema.json`.
   Select `pypi` or `testpypi` explicitly; metadata generation never searches
   or falls back to a different package index. Include at least one GitHub
   maintainer plus a documentation URL, a representative first query, and a
   concise extended description.
3. Run the deterministic checks used by CI:

   ```bash
   python -m pip install -r requirements.txt
   check-jsonschema --schemafile schema/extension.schema.json extensions/*/extension.json
   python -m scripts.build_catalog --check index.json
   python -m unittest discover -s tests -v
   ```

4. Optionally build the complete site against live metadata. `GITHUB_TOKEN`
   increases the GitHub API rate limit but is not required for public repos:

   ```bash
   GITHUB_TOKEN=$(gh auth token) python -m scripts.build_site --output _site
   ```

For TestPyPI packages, the generated installation recipes keep indexes
isolated. The service publishes separately labeled POSIX-shell and Windows
PowerShell scripts. POSIX steps set `PIP_CONFIG_FILE=/dev/null` and are joined
with `&&`; PowerShell uses its native environment-variable syntax and the
Windows `nul` device inside a guarded `try`/`finally` block that restores the
caller's prior environment. Every pip invocation also passes the `--isolated`
global option to ignore the remaining environment variables. The recipe first
uses `uv` to resolve the complete public dependency closure across the
provider's supported Python versions. It then installs that exact, wheel-only
closure from PyPI with dependency resolution disabled before installing the
exact Vane and extension wheels from TestPyPI the same way. Both steps force
reinstallation so a matching distribution already present from another source
cannot satisfy either step.
The universal resolver receives an explicit lower Python bound derived by
`dep-logic` from the provider's complete `Requires-Python` specifier set, so a
newer build interpreter cannot silently drop older-Python conditional
dependencies. An absent or unsupported lower bound fails the build.
Root requirement markers also carry the complete Python range, including upper
bounds and excluded versions, instead of relying on temporary project metadata
that `uv pip compile` does not apply to its universal resolution.
The site build fails closed if the provider or any selected Vane-owned
dependency lacks a non-yanked wheel, if an applicable internal release does not
cover every provider-wheel Python/platform environment with compatible ABI
tags, or if the public closure contains a Vane-owned package. The registry never emits
`--extra-index-url`, because pip gives no priority to the primary index and that
pattern is vulnerable to dependency confusion. PEP 508 requirements and wheel
filenames are parsed by `packaging`; `dep-logic` reduces compound extra markers
without tying recipes to the build machine. Conditions on exact Vane-owned
dependencies are preserved and propagated through their dependency graph;
different versions are accepted only when their effective conditions do not
overlap within the provider's actual wheel environments and `Requires-Python`
range. Requirements that cannot apply to any provider wheel are skipped before
fetching or validating their releases. Every selected internal release must
also support the provider environments where its incoming condition is active;
an incompatible `Requires-Python` closure fails
the build. A bounded search also requires a jointly installable selection of
the entire internal wheel closure in every modeled provider environment,
including the effective conditions of each release. Finding one working
environment, or checking dependencies only in isolation, is insufficient.
The search subtracts each covered region and continues with one shared work
budget until no unsupported provider environment remains. Each wheel file's
`Requires-Python` is intersected with its tag environment, in
addition to the release-level constraint. Files sharing a tag remain alternative
environments, including disjoint Python ranges; they are not collapsed into one
broader range. After public resolution, the exact PyPI
release wheel metadata is fetched concurrently and added to the same common-
environment coverage check, with each lock marker preserved. Before that
aggregate search, each public pin is checked against provider wheels where its
lock marker applies, using the same complete conditional-coverage validation as
internal dependencies. A universal public lock alone does not prove compatibility
with a provider's platform. Public pins
remain separate from the TestPyPI installation step. Their dependency metadata
is resolved by `uv`, not re-parsed by the Vane-owned dependency graph walker.
Native ABI validation covers CPython and PyPy's documented `pp73` ABI revision;
other interpreter families require ABI-independent (`none`) wheels. The
free-threaded Stable ABI (`abi3t`) requires runtime 3.15 or later,
following PEP 803's supported builds; experimental backports are not promised.
Installability checks currently model Linux, macOS, and Windows host families,
matching the OS families handled by Vane's extension-wheel platform policies.
Platform-independent (`any`) dependency wheels are supported too. Versioned
platform policies must use canonical version components from 0 through 99.
Linux architecture names are allowlisted; manylinux's architecture-specific
minimum glibc versions and legacy targets are checked, and musllinux wheels
must share one musl major version. macOS tags must be emitted by `packaging` for
an x86-64 or Arm64 runtime, including supported multi-architecture tags.
Other valid Python wheel platforms (such as Android, iOS, Emscripten, AIX, and
FreeBSD) may appear in informational package metadata, but cannot establish an installable
registry environment. A release available only on such platforms fails the
build explicitly. This service does not add support for new Vane host platforms
or assume that an opaque tag can satisfy arbitrary OS-dependent requirements.
Direct-URL requirements are omitted
from published JSON and HTML so artifact locations or embedded credentials
cannot leak through package metadata.

For PyPI providers, the same `uv` resolver locks the provider and its complete
dependency closure together. The registry publishes the installation command
only after the provider has a non-yanked wheel and the wheel-only universal
resolution succeeds; pip then installs the exact closure with dependency
resolution disabled.

During site generation, identical PyPI closure requests share one in-flight
resolution while distinct closures resolve concurrently with the surrounding
metadata workers; the cache mutex is never held while `uv` is running.

`index.json` is generated deterministically from the discovery subset of the
individual manifests and must be updated in the same pull request. Package
versions and wheel/Python platform availability are derived from the manifest's
explicit Python package index while GitHub stars come from the repository API.
PyPI download estimates from `pypistats.org` are published in a separate
metrics document; TestPyPI does not expose meaningful download counts, so those
values are `null`. Unavailable or invalid PyPIStats responses also produce
`null` metrics instead of blocking publication. Fetch and response-validation
errors emit a build warning; GitHub identity and package/dependency validation
errors still fail the build.
No direct artifact URL, hash, or trust identity is published
by the registry: those values belong to immutable provider packages and their
Vane descriptors.

## Layout

- `extensions/*/extension.json`: one reviewed discovery manifest per extension
- `schema/extension.schema.json`: the strict manifest schema
- `schema/*detail*.schema.json`: public enriched-detail service contracts
- `schema/download-metrics.schema.json`: public metrics service contract
- `scripts/build_catalog.py`: deterministic catalog generator and validator
- `scripts/build_site.py`: strict live-metadata enrichment and Pages assembler
- `index.json`: the reviewed aggregate consumed by Vane
- `site/`: the human-readable GitHub Pages landing page source

The Pages build produces one detail page and JSON document per extension. Each
contains the reviewed documentation, install/load examples, package publication
state, latest package version and upload time, `Requires-Python`, available
Python/ABI/platform wheel tags, validated package requirements with direct-URL
entries omitted, GitHub stars, and download metrics when the selected index
supports them. These values are informational and never participate in artifact
resolution or trust.

The repository is licensed under the Apache License 2.0. Each manifest records
the license declared by its provider project; that field does not change the
license of this registry.
