# Vane extensions registry

This repository is the public discovery registry for independently published
[Vane](https://github.com/AstroVela/vane) dynamic extension provider packages.
It plays the same narrow role as an extension directory: it tells clients which
provider package owns an extension name and where that provider is maintained.

The machine-readable catalog is published by GitHub Pages at:

```text
https://astrovela.github.io/vane-extensions/v1/index.json
```

The registry is discovery metadata only. It does not install packages, select
versions, distribute native artifacts, or grant trust to an artifact. Python
package indexes resolve and install provider wheels; Vane validates each
provider's embedded metadata and native descriptor when it is explicitly
loaded.

## Add an extension

1. Publish a Python distribution named `vane-extension-<name>` that exposes a
   `vane.dynamic_extension_providers` entry point named `<name>`.
2. Add `extensions/<name>/extension.json`, following
   `schema/extension.schema.json`.
3. Run the same checks as CI:

   ```bash
   python -m pip install check-jsonschema==0.38.0
   check-jsonschema --schemafile schema/extension.schema.json extensions/*/extension.json
   python -m scripts.build_catalog --check index.json
   python -m unittest discover -s tests -v
   ```

`index.json` is generated deterministically from the individual manifests and
must be updated in the same pull request. Do not put package versions, artifact
URLs, hashes, platform tags, or trust identities in a registry manifest; those
values belong to immutable provider packages and their Vane descriptors.

## Layout

- `extensions/*/extension.json`: one reviewed discovery manifest per extension
- `schema/extension.schema.json`: the strict manifest schema
- `scripts/build_catalog.py`: deterministic catalog generator and validator
- `index.json`: the reviewed aggregate consumed by Vane
- `site/`: the human-readable GitHub Pages landing page

The repository is licensed under the Apache License 2.0. Each manifest records
the license declared by its provider project; that field does not change the
license of this registry.
