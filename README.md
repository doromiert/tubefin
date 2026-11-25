# TubeFin

TubeFin is a native GTK 4/libadwaita desktop client for YouTube and Jellyfin. It
uses `yt-dlp` for YouTube search and stream resolution, Jellyfin's HTTP API for
your personal library, and a Cine-inspired embedded libmpv player.

It currently supports:

- YouTube search without a Google API key
- Jellyfin username/password sign-in and persistent sessions
- Jellyfin home, library, folder, and responsive series details with expandable seasons
- Conditional Seerr movie and show search/request support with Quick Connect
- Search within Jellyfin
- Thumbnail caching and native libmpv video/audio playback
- Video details, channels, authenticated comments, chapter seeking, captions, dubbed audio, and quality selection
- A mixed YouTube/Jellyfin queue with reordering, looping, prebuffering, and auto-advance
- Multi-audio YouTube and metadata-preserving Jellyfin downloads, an offline library, and local playlists
- Dedicated embedded YouTube sign-in with KeePassXC autofill; optional Google API access
- Adaptive video/audio streams at every available resolution, with a screen-matched default
- HDR10 and HLG video with SDR application chrome in the Nix build
- Reorderable, runtime-collapsible homepage sections with pull-to-refresh
- Watch-together rooms through SyncTube for YouTube and SyncPlay for Jellyfin
- Adaptive libadwaita navigation, a mini player, dark mode, and keyboard shortcuts
- Fully reproducible Nix packaging and development environment

## Run

With flakes enabled:

```sh
nix run
```

The first run may download GTK, mpv, and the required codecs. For development:

```sh
nix develop
python -m tubefin
```

Build the distributable package with:

```sh
nix build
./result/bin/tubefin
```

## Packages

The release page provides an AppImage, a Flatpak bundle, and an Arch Linux
package in addition to the Nix flake. Build them from a clean checkout with:

```sh
packaging/appimage/build.sh
packaging/flatpak/build.sh
nix shell nixpkgs#zstd --command packaging/arch/build.sh
```

The artifacts are written to `dist/`. The Flatpak build requires Flathub and
`flatpak-builder`; its script installs the matching GNOME runtime dependencies
from Flathub. Arch users can also build the native package with
`makepkg -si` from `packaging/arch`.

For each version bump, push the matching `vX.Y.Z` tag. The tag starts the release
workflow and publishes the AppImage, Flatpak bundle, and Arch package; a version
change by itself does not create a GitHub release.

## Jellyfin

Open **Jellyfin** and select **Connect to Jellyfin**. Enter a full server address,
such as `https://media.example.com` or `http://192.168.1.20:8096`, plus your
Jellyfin credentials.

The resulting access token is stored at
`$XDG_CONFIG_HOME/tubefin/config.json` (normally
`~/.config/tubefin/config.json`) with mode `0600`. Prefer HTTPS when the server is
outside your trusted local network.

## YouTube

TubeFin invokes the packaged `yt-dlp` executable. The Nix build pins resolver
2026.11.23; bump the `ytDlp` version and source hash in `flake.nix` to update it.
`nix flake update` updates the underlying runtime packages.

Use TubeFin in accordance with YouTube's terms and the laws in your jurisdiction.

### YouTube account

Open **Settings → YouTube → Login** and sign in on Google's page inside TubeFin.
Choose **Continue** after YouTube finishes signing you in. TubeFin owns this session;
it does not read Firefox or any other browser profile. **Sign Out** removes the
dedicated browser session and resolver cookies. Session data lives in the private
TubeFin configuration directory; exported cookies have mode `0600`.

Choose **Fill from KeePassXC** to use a matching Google login. KeePassXC (or Keyslot)
must be running with Browser Integration enabled. Approve TubeFin in its connection
dialog the first time. Autofill runs only on `https://accounts.google.com`, never
submits the form, and does not save the password in TubeFin. Choose a login once; it carries through Google's separate email and password steps
automatically. TubeFin stores only the public KeePassXC association identifier.

### Optional Google API access

Optional API features require a Google OAuth **Desktop app** client ID with the YouTube
Data API enabled. Open **YouTube account**, paste the client ID, and choose either
standard access with commenting or the explicit playlist-management scope. Authorization uses PKCE
and a temporary loopback callback. Refresh tokens are stored through Secret Service
(`libsecret`), never in `config.json`; signing out revokes the grant and removes the
keyring entry.

YouTube's public Data API does not expose a complete watch-history feed. TubeFin shows
account activity where the API provides it and keeps its own device-local playback
history for Continue Watching and signed-out channel suggestions. Signed-in online
playback offers a Resume toast for local or YouTube account-saved progress. If both
positions exist, the toast offers the furthest one. Playback starts at the beginning
unless you choose Resume. Videos are automatically
marked fully watched only at their end.

### Resolution and HDR

Automatic quality uses the physical pixel height of the monitor containing the
TubeFin window when playback starts, including fractional scaling. It selects the
highest available resolution that fits that screen. Signed-in playback combines
account metadata with public adaptive streams, so account-client restrictions do
not hide working 4K/HDR choices for public videos. Every available resolution
remains selectable manually, including 4K and 8K. Separate video and original-audio
streams are played together; HDR variants are labelled in the quality menu.

The Nix package includes an application-local GTK 4.22 backport that keeps windows,
menus and controls sRGB/8-bit while explicitly PQ/HLG-tagged video uses an independent
PQ float16 surface. HLG is converted to PQ by mpv. HDR requires a compatible Wayland
compositor and an HDR-enabled display. Unsupported output paths use GTK's SDR
fallback. A 10-bit SDR video does not enable HDR. Other distribution builds require
the same GTK backport for this media-only HDR path.

## Offline media and playlists

Downloads and their metadata live under `$XDG_DATA_HOME/tubefin/downloads` (normally
`~/.local/share/tubefin/downloads`). The Downloads page reports storage usage and supports
search, playback, retry, cancellation, moved-file recovery, and deletion. Local
playlists can mix YouTube, Jellyfin, and downloaded items. Pasting a YouTube playlist
URL browses it and places its videos into the unified queue.

Completed downloads retain their original source link. When the same YouTube video
appears elsewhere in TubeFin, playback prefers the local file and offers online streams
as alternate quality choices. The download dialog can mux multiple available dubbed
audio tracks into one MKV file.

## Watch together

Choose **Watch together** in the player to create or join a SyncTube room for YouTube,
or a Jellyfin SyncPlay room for media on your server. SyncTube temporarily hides
Jellyfin content, while a Jellyfin room temporarily hides YouTube recommendations, so
each room only exposes media its backend can synchronize.

## Keyboard shortcuts

- <kbd>Ctrl</kbd>+<kbd>F</kbd> or <kbd>Ctrl</kbd>+<kbd>L</kbd> — focus search
- <kbd>F</kbd> — toggle fullscreen
- <kbd>Space</kbd> — play or pause
- <kbd>H</kbd>/<kbd>L</kbd> or arrow keys — seek backward or forward
- <kbd>Ctrl</kbd> plus seek keys — previous or next video
- <kbd>Esc</kbd> — leave the full player while playback continues
- <kbd>Ctrl</kbd>+<kbd>Q</kbd> — quit

## Architecture

The dedicated login uses WebKitGTK 6 with its own persistent network session.
Media and library requests use the Python standard library and websocket-client on
worker threads; GTK updates return to the main loop. Video is rendered by libmpv
into a GTK GLArea using the approach developed by Cine. The GTK HDR backport derives
from Galeria's media-only HDR work; its source-license attribution is retained in
`gtk-media-hdr.patch`.

## Known limitations

- Jellyfin transcoding negotiation is limited; direct playback is preferred.
- Music libraries and audio-only Jellyfin playback are intentionally unsupported.
- Direct playback depends on codecs supported by the packaged mpv/FFmpeg build.
- Jellyfin credentials use its access token in a permission-restricted config file; Google
  refresh tokens use the system keyring.
- YouTube extraction, comments, and downloads depend on the packaged `yt-dlp` remaining
  compatible with YouTube.

TubeFin is not affiliated with YouTube, Google, or Jellyfin.

