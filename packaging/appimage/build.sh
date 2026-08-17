#!/bin/sh
set -eu

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
output_directory=${1:-"$repository_root/dist"}
version=1.4.0

mkdir -p "$output_directory"
work_directory=$(mktemp -d)
appdir="$work_directory/AppDir"
trap 'rm -rf "$work_directory"' EXIT INT TERM

# 1. Structure the AppDir
mkdir -p "$appdir/usr/lib/tubefin" "$appdir/usr/bin" "$appdir/usr/share/applications" "$appdir/usr/share/icons/hicolor/scalable/apps"

cp -a "$repository_root/src/tubefin" "$appdir/usr/lib/tubefin/"
find "$appdir/usr/lib/tubefin" -type d -name __pycache__ -prune -exec rm -rf {} +

# 2. Vendor python dependencies directly into the AppDir
python3 -m pip install \
  --target="$appdir/usr/lib/tubefin" \
  --break-system-packages \
  --no-cache-dir \
  websocket-client python-mpv yt-dlp

install -Dm644 "$repository_root/data/io.github.doromiert.TubeFin.desktop" "$appdir/usr/share/applications/io.github.doromiert.TubeFin.desktop"
install -Dm644 "$repository_root/data/io.github.doromiert.TubeFin.svg" "$appdir/usr/share/icons/hicolor/scalable/apps/io.github.doromiert.TubeFin.svg"
cp "$repository_root/data/io.github.doromiert.TubeFin.svg" "$appdir/io.github.doromiert.TubeFin.svg"
cp "$repository_root/data/io.github.doromiert.TubeFin.desktop" "$appdir/io.github.doromiert.TubeFin.desktop"

# 3. Launcher entrypoint
cat << 'EOF' > "$appdir/AppRun"
#!/bin/sh
HERE="$(dirname "$(readlink -f "${0}")")"
export PYTHONPATH="$HERE/usr/lib/tubefin:$PYTHONPATH"
exec python3 -m tubefin "$@"
EOF
chmod +x "$appdir/AppRun"

# 4. Fetch appimagetool and bundle
wget -q https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage -O "$work_directory/appimagetool"
chmod +x "$work_directory/appimagetool"

ARCH=x86_64 VERSION="$version" "$work_directory/appimagetool" "$appdir" "$output_directory/TubeFin-$version-x86_64.AppImage"
