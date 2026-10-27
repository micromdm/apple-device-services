# The jsonschema CLI of https://github.com/sourcemeta/jsonschema (not the
# Python package of the same name).
JSONSCHEMA ?= jsonschema

all: generate

generate: generate-anb generate-abm generate-dep generate-asm

fetch: fetch-anb fetch-abm fetch-dep fetch-asm

generate-dep:
	tools/dev_docs_schema.py generate devicemanagement/device-assignment.json dep/schemas
	rm dep/schemas/MachineInfo.json dep/schemas/SkipKeys.json
	tools/dev_docs_schema.py generate devicemanagement/roster-management.json dep/schemas
	tools/sortkeys.py dep/patches/*.json
	tools/apply_schema_patches.py dep/patches dep/schemas
	$(JSONSCHEMA) fmt dep/schemas

fetch-dep:
	tools/dev_docs_schema.py fetch devicemanagement/device-assignment.json
	tools/dev_docs_schema.py fetch devicemanagement/roster-management.json

generate-abm:
	tools/dev_docs_schema.py generate applebusinessapi.json abm/schemas
	tools/sortkeys.py abm/patches/*.json
	tools/apply_schema_patches.py abm/patches abm/schemas
	$(JSONSCHEMA) fmt abm/schemas

fetch-abm:
	tools/dev_docs_schema.py fetch applebusinessapi.json

generate-anb:
	tools/dev_docs_schema.py generate devicemanagement/app-book-and-subscription-management.json anb/schemas
	$(JSONSCHEMA) fmt anb/schemas

fetch-anb:
	tools/dev_docs_schema.py fetch devicemanagement/app-book-and-subscription-management.json

generate-asm:
	tools/dev_docs_schema.py generate appleschoolmanagerapi.json asm/schemas
	tools/sortkeys.py asm/patches/*.json
	tools/apply_schema_patches.py asm/patches asm/schemas
	$(JSONSCHEMA) fmt asm/schemas

fetch-asm:
	tools/dev_docs_schema.py fetch appleschoolmanagerapi.json

format-other:
	tools/sortkeys.py other/schemas/*.json
	$(JSONSCHEMA) fmt other/schemas

.PHONY: all generate-dep fetch-dep generate-abm fetch-abm generate-anb fetch-anb generate-asm fetch-asm generate fetch format-other
