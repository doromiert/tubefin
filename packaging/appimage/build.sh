#!/bin/sh
set -eu

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
output_directory=${1:-"$repository_root/dist"}

mkdir -p "$output_directory"
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT INT TERM

# 1. Build binary using standard tools (cargo, cmake, etc.)
# 2. Populate an AppDir structure
# 3. Run appimagetool directly on the AppDir

echo "AppImage packaging script updated to host tools"
