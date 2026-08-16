#!/bin/sh
set -eu

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
output_directory=${1:-"$repository_root/dist"}
bundler=github:ralismark/nix-appimage/7946addbc0d97e358a6d7aefe5e82310f0fe6b18

mkdir -p "$output_directory"
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT INT TERM

(
  cd "$temporary_directory"
  nix bundle --bundler "$bundler" "$repository_root#default"
  appimage=$(find . -maxdepth 1 \( -type f -o -type l \) -name '*.AppImage' -print -quit)
  test -n "$appimage" && test -e "$appimage"
  install -Dm755 "$appimage" "$output_directory/TubeFin-1.4.0-x86_64.AppImage"
)
