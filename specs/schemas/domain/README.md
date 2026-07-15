# Domain schema compatibility

`domain-v1.schema.json` is the generated, provisional `0.3.0` contract for the
PowerFactory-independent domain package. It is not an accepted live
PowerFactory compatibility claim.

Compatibility rules:

- a minor version may add an optional field whose absence preserves existing semantics;
- a patch version may clarify metadata without changing admitted instances or canonical bytes;
- removing or renaming a field, changing a type, making a field required, or changing field semantics requires a major version;
- changing canonical JSON, digest membership, identity scope, locator trust, or cursor binding requires a major version;
- readers must reject unknown major versions and must not reinterpret stored values under a later schema.

Regeneration is an exact gate:

```shell
uv run python scripts/schemas/generate_domain_schemas.py --check
```

The contract suite also applies `jsonschema.Draft202012Validator` to positive
and negative fixtures. Runtime dataclass constructors remain responsible for
cross-field invariants that JSON Schema cannot express here.
