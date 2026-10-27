# Apple Device Services

[![CI/CD](https://github.com/micromdm/apple-device-services/actions/workflows/on-push-pr.yml/badge.svg)](https://github.com/micromdm/apple-device-services/actions/workflows/on-push-pr.yml)

API schema definitions for Apple's various device management web services.

While Apple does provide documentation in the form of HTML (i.e. for humans to read), machine-readable API schema definitions are much preferred for e.g. validation, code generation, etc. We love the documentation Apple provides in the Apple [Device Management](https://github.com/apple/device-management) repository and hope one day they will choose to manage and provide machine-readable schemas for their web service APIs, too. Hopefully these work as a stop-gap until that happens. These schemas were originally hand-crafted from the HTML/JavaScript documentation. The API schemas are now generated from Apple's machine-readable documentation source, the JSON behind the rendered pages (see the [retrieval documentation](docs/apple-developer-docs-retrieval.md) and the [generation tool](tools/dev_docs_schema.py)), plus a small set of [patches](#patches) for the details Apple's documentation leaves out.

## Schemas

A general list of schemas managed here:

- [ABM Schemas](abm/schemas) and [patches](abm/patches)  
[JSON Schema](https://json-schema.org) definitions for the [Apple Business Manager API](https://developer.apple.com/documentation/applebusinessapi)
- [ASM Schemas](asm/schemas) and [patches](asm/patches)  
[JSON Schema](https://json-schema.org) definitions for the [Apple School Manager API](https://developer.apple.com/documentation/appleschoolmanagerapi)
- [DEP + Roster Schemas](dep/schemas) and [patches](dep/patches)  
[JSON Schema](https://json-schema.org) definitions for the [Device Assignment (DEP)](https://developer.apple.com/documentation/devicemanagement/device-assignment) and [Roster Management](https://developer.apple.com/documentation/devicemanagement/roster-management) APIs
- [ANB Schemas](anb/schemas)  
[JSON Schema](https://json-schema.org) definitions for the [App and Book Management (ANB) API](https://developer.apple.com/documentation/devicemanagement/app-and-book-management)
- [Other schemas](other/schemas)  
[JSON Schema](https://json-schema.org) definitions for miscellaneous Apple services such as [GDMF](https://support.apple.com/guide/deployment/device-management-deploy-software-updates-depafd2fad80/web).

## Patches

Apple's documentation doesn't capture everything: some required fields, enum values, string lengths, formats, and examples are missing. We add those as patches rather than editing the generated schemas, which would be overwritten the next time they're regenerated.

Each schema directory has a `patches/` folder beside it, with a [JSON Patch](https://www.rfc-editor.org/rfc/rfc6902) (RFC 6902) for each schema that needs one, named after the schema it changes. The patches are applied each time the schemas are regenerated from Apple's documentation.

A patch checks the values it changes before changing them. If Apple's documentation changes underneath a patch, generation fails instead of quietly overwriting the new content, and the patch needs updating. To change a schema, edit its patch, not the generated file.

## Contributing

These schemas are only as complete as Apple's documentation and our patches, so they're almost guaranteed to be missing something. So: **your help needed!** If something you need is missing or wrong, PRs are welcome. For a generated schema, add or update its [patch](#patches) rather than editing the schema itself, or improve the [generation tool](tools/dev_docs_schema.py) if Apple's documentation already has what's needed.

Note that we use the [jsonschema](https://github.com/sourcemeta/jsonschema) tool to enforce JSON Schema formatting and linting.
