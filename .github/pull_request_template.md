## Summary

- 

## Registry change

- Extension name:
- Provider distribution:
- Provider repository:

## Validation

- [ ] `check-jsonschema --schemafile schema/extension.schema.json extensions/*/extension.json`
- [ ] `python -m scripts.build_catalog --check index.json`
- [ ] `python -m unittest discover -s tests -v`

## Checklist

- [ ] The provider exposes a matching `vane.dynamic_extension_providers` entry point.
- [ ] The manifest contains discovery metadata only.
- [ ] `index.json` was generated from the manifests and was not edited independently.
