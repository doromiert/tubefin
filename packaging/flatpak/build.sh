#!/bin/sh
set -eu

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
output_directory=${1:-"$repository_root/dist"}
build_directory="$repository_root/.flatpak-builder-tubefin"
repository_directory="$repository_root/.flatpak-repo-tubefin"

mkdir -p "$output_directory"
flatpak-builder \
  --force-clean \
  --user \
  --install-deps-from=flathub \
  --repo="$repository_directory" \
  "$build_directory" \
  "$repository_root/io.github.doromiert.TubeFin.yaml"
flatpak build-bundle \
  "$repository_directory" \
  "$output_directory/TubeFin-1.3.1.flatpak" \
  io.github.doromiert.TubeFin \
  stable \
  --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo
