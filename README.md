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

For TestPyPI packages, the generated installation recipe keeps indexes
isolated. Every command sets `PIP_CONFIG_FILE=/dev/null` so pip skips global,
site, user, and explicit configuration files, then passes pip's `--isolated`
global option to ignore the remaining environment variables. The recipe first
uses `uv` to resolve the complete public dependency closure across the
provider's supported Python versions. It then installs that exact, wheel-only
closure from PyPI with dependency resolution disabled before installing the
exact Vane and extension wheels from TestPyPI the same way. Both steps force
reinstallation so a matching distribution already present from another source
cannot satisfy either step. The site build fails closed unless the provider and
every selected Vane-owned dependency publish at least one non-yanked wheel, or
if the public closure contains a Vane-owned package. The registry never emits
`--extra-index-url`, because pip gives no priority to the primary index and that
pattern is vulnerable to dependency confusion. PEP 508 requirements and wheel
filenames are parsed by `packaging`; `dep-logic` reduces compound extra markers
without tying recipes to the build machine. Direct-URL requirements are omitted
from published JSON and HTML so artifact locations or embedded credentials
cannot leak through package metadata.

`index.json` is generated deterministically from the discovery subset of the
individual manifests and must be updated in the same pull request. Package
versions and wheel/Python platform availability are derived from the manifest's
explicit Python package index while GitHub stars come from the repository API.
PyPI download estimates from `pypistats.org` are published in a separate
metrics document; TestPyPI does not expose meaningful download counts, so those
values are `null`. No direct artifact URL, hash, or trust identity is published
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
