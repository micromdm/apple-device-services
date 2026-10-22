# Apple Device Services

[![CI/CD](https://github.com/micromdm/apple-device-services/actions/workflows/on-push-pr.yml/badge.svg)](https://github.com/micromdm/apple-device-services/actions/workflows/on-push-pr.yml)

API schema definitions for Apple's various device management web services.

While Apple does provide documentation in the form of HTML (i.e. for humans to read), machine-readable API schema definitions are much preferred for e.g. validation, code generation, etc. We love the documentation Apple provides in the Apple [Device Management](https://github.com/apple/device-management) repository and hope one day they will choose to manage and provide machine-readable schemas for their web service APIs, too. Hopefully these work as a stop-gap until that happens. These schemas were originally hand-crafted from the HTML/JavaScript documentation. Some are now generated from Apple's machine-readable documentation source (the JSON behind the rendered pages; see the [retrieval documentation](docs/apple-developer-docs-retrieval.md) and the [generation tool](tools/dev_docs_schema.py)), with manual curation retained for the details Apple doesn't expose.

## Schemas

A general list of schemas managed here:

- [ABM Schemas](abm/schemas)  
[JSON Schema](https://json-schema.org) definitions for the [Apple Business Manager API](https://developer.apple.com/documentation/applebusinessapi)
- [DEP Schemas](dep/schemas)  
[JSON Schema](https://json-schema.org) definitions for the [Device Assignment (DEP) API](https://developer.apple.com/documentation/devicemanagement/device-assignment)
- [ANB Schemas](anb/schemas)  
[JSON Schema](https://json-schema.org) definitions for the [App and Book Management (ANB) API](https://developer.apple.com/documentation/devicemanagement/app-and-book-management)
- [Other schemas](other/schemas)  
[JSON Schema](https://json-schema.org) definitions for miscellaneous Apple services such as [GDMF](https://support.apple.com/guide/deployment/device-management-deploy-software-updates-depafd2fad80/web).

## Patches

Regenerating schemas from Apple's documentation overwrites the manual curation its machine-readable source doesn't expose. That curation lives in a `patches/` folder beside each schema directory — one [JSON Patch](https://www.rfc-editor.org/rfc/rfc6902) (RFC 6902) per schema, named the same way as the schema it patches — and is re-applied after every generation, keeping the generated files a pure product of Apple's documentation with the curated detail layered on top.

Patches are pinned to the values they adjust: when Apple's documentation changes beneath one, the sync fails rather than apply over it. Changes to a schema's curated content are made in its patch, not in the generated file, so they survive the next regeneration.

## Contributing

These schemas are almost guaranteed to be incomplete due to the fact they are used as-needed and manually curated, with some schemas now generated from Apple's documentation via the [generation tool](tools/dev_docs_schema.py). So: **your help needed!**. Take a look at a given schema and if something you need isn't there: PRs welcome!

Note that we use the [jsonschema](https://github.com/sourcemeta/jsonschema) tool to enforce JSON Schema formatting and linting.