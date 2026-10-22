#!/usr/bin/env bash

set -e

# Grab the directory from the first argument
TARGET_DIR="$1"

# Check if the directory actually exists
if [ ! -d "$TARGET_DIR" ]; then
    echo "Error: Directory '$TARGET_DIR' does not exist." >&2
    exit 1
fi

echo "Sorting JSON keys in files in: $TARGET_DIR"

# Loop through all JSON files
for file in "$TARGET_DIR"/*.json; do
    # Check if any JSON files actually matched the wildcard
    if [ -e "$file" ]; then
        jq -S . "$file" > "$file.tmp"
        mv "$file.tmp" "$file"
    else
        echo "No JSON files found in $TARGET_DIR."
    fi
done