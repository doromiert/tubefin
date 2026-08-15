# TubeFin

TubeFin is a native GTK 4/libadwaita desktop client for YouTube and Jellyfin. It
uses `yt-dlp` for YouTube search and stream resolution, Jellyfin's HTTP API for
your personal library, and a Cine-inspired embedded libmpv player.

This is an early functional preview. It currently supports:

- YouTube search without a Google API key
- Jellyfin username/password sign-in and persistent sessions
- Jellyfin home, library, folder, series, and season browsing
- Search within Jellyfin
- Thumbnail caching and native libmpv video/audio playback
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

- YouTube account sign-in, subscriptions, comments, and playlists are not yet implemented.
- Jellyfin transcoding negotiation and playback progress reporting are not yet implemented.
- Direct playback depends on codecs supported by the packaged mpv/FFmpeg build.
- The Jellyfin token currently lives in a permission-restricted config file rather than a keyring.

TubeFin is not affiliated with YouTube, Google, or Jellyfin.
