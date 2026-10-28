# The jsonschema CLI of https://github.com/sourcemeta/jsonschema (not the
# Python package of the same name).
JSONSCHEMA ?= jsonschema

all: generate

generate: generate-anb generate-abm generate-dep generate-asm

fetch: fetch-anb fetch-abm fetch-dep fetch-asm

generate-dep:
	tools/dev_docs_schema.py generate devicemanagement/device-assignment.json devicemanagement/roster-management.json dep/schemas
	# No prune here: dep/ has two roots and prune takes one source (see #25).
	rm dep/schemas/MachineInfo.json dep/schemas/SkipKeys.json
	tools/sortkeys.py dep/patches/*.json
	tools/apply_schema_patches.py dep/patches dep/schemas
	$(JSONSCHEMA) fmt dep/schemas -i dep/schemas/OpenAPI.json

fetch-dep:
	tools/dev_docs_schema.py fetch devicemanagement/device-assignment.json
	tools/dev_docs_schema.py fetch devicemanagement/roster-management.json

generate-abm:
	tools/dev_docs_schema.py generate applebusinessapi.json abm/schemas
	tools/dev_docs_schema.py prune applebusinessapi.json abm/schemas
	tools/sortkeys.py abm/patches/*.json
	tools/apply_schema_patches.py abm/patches abm/schemas
	$(JSONSCHEMA) fmt abm/schemas -i abm/schemas/OpenAPI.json

fetch-abm:
	tools/dev_docs_schema.py fetch applebusinessapi.json

generate-anb:
	tools/dev_docs_schema.py generate devicemanagement/app-book-and-subscription-management.json anb/schemas
	tools/dev_docs_schema.py prune devicemanagement/app-book-and-subscription-management.json anb/schemas
	tools/sortkeys.py anb/patches/*.json
	tools/apply_schema_patches.py anb/patches anb/schemas
	$(JSONSCHEMA) fmt anb/schemas -i anb/schemas/OpenAPI.json

fetch-anb:
	tools/dev_docs_schema.py fetch devicemanagement/app-book-and-subscription-management.json

generate-asm:
	tools/dev_docs_schema.py generate appleschoolmanagerapi.json asm/schemas
	tools/dev_docs_schema.py prune appleschoolmanagerapi.json asm/schemas
	tools/sortkeys.py asm/patches/*.json
	tools/apply_schema_patches.py asm/patches asm/schemas
	$(JSONSCHEMA) fmt asm/schemas -i asm/schemas/OpenAPI.json

fetch-asm:
	tools/dev_docs_schema.py fetch appleschoolmanagerapi.json

format-other:
	tools/sortkeys.py other/schemas/*.json
	$(JSONSCHEMA) fmt other/schemas -i other/schemas/OpenAPI.json

# The OpenAPI documents beside the schemas are not JSON Schemas.
lint:
	$(JSONSCHEMA) lint */schemas $$(for f in */schemas/OpenAPI.json; do echo "-i $$f"; done)
	$(JSONSCHEMA) fmt -c */schemas $$(for f in */schemas/OpenAPI.json; do echo "-i $$f"; done)
	tools/sortkeys.py --check */patches/*.json
	tools/sortkeys.py --check */schemas/OpenAPI.json

.PHONY: all generate-dep fetch-dep generate-abm fetch-abm generate-anb fetch-anb generate-asm fetch-asm generate fetch format-other lint
