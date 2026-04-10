#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENGINE_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT="${1:-/tmp/engine-code-bundle.tar.gz}"

echo "Packaging engine source code..."
cd "$ENGINE_DIR"

# Package only the src/ directory (proprietary code)
tar czf "$OUTPUT" src/

echo "Created: $OUTPUT"
echo "Contents:"
tar tzf "$OUTPUT" | head -20
echo "..."
echo "Total files: $(tar tzf "$OUTPUT" | wc -l)"
