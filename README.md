# Apple Device Services

[![CI/CD](https://github.com/micromdm/apple-device-services/actions/workflows/on-push-pr.yml/badge.svg)](https://github.com/micromdm/apple-device-services/actions/workflows/on-push-pr.yml)

API schema definitions for Apple's various device management web services.

While Apple does provide documentation in the form of HTML (i.e. for humans to read), machine-readable API schema definitions are much preferred for e.g. validation, code generation, etc. We love the documentation Apple provides in the Apple [Device Management](https://github.com/apple/device-management) repository and hope one day they will choose to manage and provide machine-readable schemas for their web service APIs, too. Hopefully these work as a stop-gap until that happens. These schemas were hand-crafted from the HTML documentation.

## Schemas

A general list of schemas managed here:

- [ABM Schemas](abm/schemas)  
[JSON Schema](https://json-schema.org) definitions for the [Apple Business Manager API](https://developer.apple.com/documentation/applebusinessmanagerapi)
- [DEP Schemas](dep/schemas)  
[JSON Schema](https://json-schema.org) definitions for the [Device Assignment (DEP) API](https://developer.apple.com/documentation/devicemanagement/device-assignment)
- [ANB Schemas](anb/schemas)  
[JSON Schema](https://json-schema.org) definitions for the [App and Book Management (ANB) API](https://developer.apple.com/documentation/devicemanagement/app-and-book-management)
- [Other schemas](other/schemas)  
[JSON Schema](https://json-schema.org) miscellaneous Apple services such as [GDMF](https://support.apple.com/guide/deployment/device-management-deploy-software-updates-depafd2fad80/web).

## Contributing

These schemas are almost guaranteed to be incomplete due to the fact they are used as-needed and manually generated from Apple's documentation. So: **your help needed!**. Take a look at a given schema and if something you need isn't there: PRs welcome!

Note that we use the [jsonschema](https://github.com/sourcemeta/jsonschema) tool to enforce JSON Schema formatting and linting.