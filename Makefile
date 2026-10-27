all: generate

generate: generate-anb generate-abm generate-dep

fetch: fetch-anb fetch-abm fetch-dep

generate-dep:
	tools/dev_docs_schema.py generate devicemanagement/device-assignment.json dep/schemas
	rm dep/schemas/MachineInfo.json dep/schemas/SkipKeys.json
	tools/dev_docs_schema.py generate devicemanagement/accountdetail.json dep/schemas
	tools/sortkeys.py dep/patches/*.json
	tools/apply_schema_patches.py dep/patches dep/schemas
	tools/sortkeys.py dep/schemas/*.json
	jsonschema fmt dep/schemas

fetch-dep:
	tools/dev_docs_schema.py fetch devicemanagement/device-assignment.json
	tools/dev_docs_schema.py fetch devicemanagement/accountdetail.json

generate-abm:
	tools/dev_docs_schema.py generate applebusinessapi.json abm/schemas
	tools/sortkeys.py abm/patches/*.json
	tools/apply_schema_patches.py abm/patches abm/schemas
	tools/sortkeys.py abm/schemas/*.json
	jsonschema fmt abm/schemas

fetch-abm:
	tools/dev_docs_schema.py fetch applebusinessapi.json

generate-anb:
	tools/dev_docs_schema.py generate devicemanagement/app-book-and-subscription-management.json anb/schemas
	tools/sortkeys.py anb/patches/*.json
	tools/apply_schema_patches.py anb/patches anb/schemas
	tools/sortkeys.py anb/schemas/*.json
	jsonschema fmt anb/schemas

fetch-anb:
	tools/dev_docs_schema.py fetch devicemanagement/app-book-and-subscription-management.json

.PHONY: all generate-dep fetch-dep generate-abm fetch-abm generate-anb fetch-anb generate fetch
