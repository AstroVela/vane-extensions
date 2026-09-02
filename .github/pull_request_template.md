## Summary

- 

## Registry change

- Extension name:
- Provider distribution:
- Provider repository:
- Package index:
- Documentation URL:

## Validation

- [ ] `check-jsonschema --schemafile schema/extension.schema.json extensions/*/extension.json`
- [ ] `python -m scripts.build_catalog --check index.json`
- [ ] `python -m unittest discover -s tests -v`
- [ ] `GITHUB_TOKEN=$(gh auth token) python -m scripts.build_site --output _site`

## Checklist

- [ ] The provider exposes a matching `vane.dynamic_extension_providers` entry point.
- [ ] The manifest contains reviewed discovery and documentation metadata only.
- [ ] The package index and GitHub maintainers are explicit and correct.
- [ ] The examples do not contain credentials, private endpoints, or personal paths.
- [ ] `index.json` was generated from the manifests and was not edited independently.
- [ ] Derived versions, wheel tags, stars, and metrics remain outside the stable discovery catalog.
