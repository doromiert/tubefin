#!/bin/sh
set -eu

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
output_directory=${1:-"$repository_root/dist"}
version=1.3.1-1
epoch=${SOURCE_DATE_EPOCH:-$(git -C "$repository_root" log -1 --format=%ct)}
work_directory=$(mktemp -d)
package_root="$work_directory/package"
trap 'rm -rf "$work_directory"' EXIT INT TERM

install -d "$package_root/usr/lib/tubefin"
cp -a "$repository_root/src/tubefin" "$package_root/usr/lib/tubefin/"
find "$package_root/usr/lib/tubefin" -type d -name __pycache__ -prune -exec rm -rf {} +
install -Dm755 "$repository_root/packaging/arch/tubefin" "$package_root/usr/bin/tubefin"
install -Dm644 "$repository_root/data/io.github.doromiert.TubeFin.desktop" \
  "$package_root/usr/share/applications/io.github.doromiert.TubeFin.desktop"
install -Dm644 "$repository_root/data/io.github.doromiert.TubeFin.metainfo.xml" \
  "$package_root/usr/share/metainfo/io.github.doromiert.TubeFin.metainfo.xml"
install -Dm644 "$repository_root/data/io.github.doromiert.TubeFin.svg" \
  "$package_root/usr/share/icons/hicolor/scalable/apps/io.github.doromiert.TubeFin.svg"

size=$(du -sb "$package_root" | cut -f1)
cat > "$package_root/.PKGINFO" <<EOF
pkgname = tubefin
pkgbase = tubefin
pkgver = $version
pkgdesc = Native GTK YouTube and Jellyfin client
url = https://github.com/doromiert/tubefin
builddate = $epoch
packager = TubeFin release build
size = $size
arch = any
license = GPL-3.0-or-later
depend = ffmpeg
depend = deno
depend = gst-libav
depend = gst-plugins-bad
depend = gst-plugins-base
depend = gst-plugins-good
depend = gst-plugins-ugly
depend = gtk4
depend = libadwaita
depend = libsecret
depend = mpv
depend = python
depend = python-gobject
depend = python-mpv
depend = python-websocket-client
depend = yt-dlp
EOF

mkdir -p "$output_directory"
tar \
  --sort=name \
  --mtime="@$epoch" \
  --owner=0 \
  --group=0 \
  --numeric-owner \
  -I 'zstd -19 -T0' \
  -C "$package_root" \
  -cf "$output_directory/tubefin-$version-any.pkg.tar.zst" \
  .
