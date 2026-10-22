all: generate-dep

generate-dep:
	tools/dev_docs_schema.py generate devicemanagement/device-assignment.json dep/schemas
	# remove unneeded generated schema files
	rm dep/schemas/MachineInfo.json dep/schemas/SkipKeys.json
	tools/dev_docs_schema.py generate devicemanagement/accountdetail.json dep/schemas
	tools/sort.sh dep/patches
	tools/apply_schema_patches.py dep/patches dep/schemas
	tools/sort.sh dep/schemas
	jsonschema fmt dep/schemas

fetch-dep:
	tools/dev_docs_schema.py fetch devicemanagement/device-assignment.json
	tools/dev_docs_schema.py fetch devicemanagement/accountdetail.json

.PHONY: all generate-dep fetch-dep
