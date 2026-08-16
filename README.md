# TubeFin

TubeFin is a native GTK 4/libadwaita desktop client for YouTube and Jellyfin. It
uses `yt-dlp` for YouTube search and stream resolution, Jellyfin's HTTP API for
your personal library, and a Cine-inspired embedded libmpv player.

It currently supports:

- YouTube search without a Google API key
- Jellyfin username/password sign-in and persistent sessions
- Jellyfin home, library, folder, series, and season browsing
- Search within Jellyfin
- Thumbnail caching and native libmpv video/audio playback
- Video details, channels, paginated comments/replies, captions, and quality selection
- A mixed YouTube/Jellyfin queue with reordering, looping, prebuffering, and auto-advance
- Quality-selectable YouTube downloads, an offline library, and local playlists
- Google desktop OAuth with subscriptions, liked videos, activity, and playlist management
- Local-history and signed-in homepage sections with explicit recommendation provenance
- Synchronized playback rooms with host permissions and smooth drift correction
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

## Jellyfin

Open **Jellyfin** and select **Connect to Jellyfin**. Enter a full server address,
such as `https://media.example.com` or `http://192.168.1.20:8096`, plus your
Jellyfin credentials.

The resulting access token is stored at
`$XDG_CONFIG_HOME/tubefin/config.json` (normally
`~/.config/tubefin/config.json`) with mode `0600`. Prefer HTTPS when the server is
outside your trusted local network.

## YouTube

TubeFin invokes the packaged `yt-dlp` executable. YouTube changes frequently, so
updating the flake input is the normal way to pick up a newer resolver:

```sh
nix flake update
```

Use TubeFin in accordance with YouTube's terms and the laws in your jurisdiction.

### YouTube account

Account features require a Google OAuth **Desktop app** client ID with the YouTube
Data API enabled. Open **YouTube account**, paste the client ID, and choose either
read-only access or the explicit playlist-management scope. Authorization uses PKCE
and a temporary loopback callback. Refresh tokens are stored through Secret Service
(`libsecret`), never in `config.json`; signing out revokes the grant and removes the
keyring entry.

YouTube's public Data API does not expose a complete watch-history feed. TubeFin shows
account activity where the API provides it and keeps its own device-local playback
history for Continue Watching and signed-out channel suggestions.

## Offline media and playlists

Downloads and their metadata live under `$XDG_DATA_HOME/tubefin/downloads` (normally
`~/.local/share/tubefin/downloads`). The Offline page reports storage usage and supports
search, playback, retry, cancellation, moved-file recovery, and deletion. Local
playlists can mix YouTube, Jellyfin, and downloaded items. Pasting a YouTube playlist
URL browses it and places its videos into the unified queue.

## Synchronized playback

Run the independently deployable room server with:

```sh
tubefin-room-server --host 0.0.0.0 --port 8765
```

Then choose **Synchronized Playback** from the app menu to create or join a room. The
host controls the shared media state and can grant control to participant client IDs.
The protocol synchronizes media identity, pause, seeks, and position, estimates clock
offset, applies small temporary speed corrections, and reconnects participants to the
last room state. Expose the TCP server only through a trusted network or a secured
tunnel; the compact reference server does not terminate TLS.

## Keyboard shortcuts

- <kbd>Ctrl</kbd>+<kbd>L</kbd> — focus search
- <kbd>Esc</kbd> — leave the full player while playback continues
- <kbd>Ctrl</kbd>+<kbd>Q</kbd> — quit

## Architecture

The app intentionally has no embedded browser and no third-party Python runtime
dependencies beyond PyGObject and python-mpv. Network calls use the Python standard
library and run on worker threads; all GTK updates return to the main loop. Video is
rendered by libmpv into a GTK GLArea using the approach developed by Cine.

## Known limitations

- Jellyfin transcoding negotiation and playback progress reporting are not yet implemented.
- Direct playback depends on codecs supported by the packaged mpv/FFmpeg build.
- Jellyfin credentials use its access token in a permission-restricted config file; Google
  refresh tokens use the system keyring.
- YouTube extraction, comments, and downloads depend on the packaged `yt-dlp` remaining
  compatible with YouTube.

TubeFin is not affiliated with YouTube, Google, or Jellyfin.
