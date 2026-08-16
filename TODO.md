# TubeFin roadmap

Implementation order reflects dependencies, accessibility, and user value.

## 1. Player foundation

- [x] List authored and automatic caption tracks for YouTube videos.
- [x] List Jellyfin subtitle tracks exposed by the media source.
- [x] Select, disable, and switch captions during playback.
- [x] List playable qualities in the player for YouTube videos.
- [x] Switch quality without losing the current position.
- [x] Add a persistent buffer-duration setting.
- [x] Keep sensible automatic defaults for quality and buffering.
- [x] Move quality, captions, and buffering into a player settings menu.
- [x] Filter unusable generated caption translations.
- [x] Prebuffer up to 10 seconds for visible, likely-next videos with bounded concurrency.

## 1.5. Unified queue

- [x] Add one queue containing both YouTube and Jellyfin items.
- [x] Add, remove, reorder, clear, and play queue entries.
- [x] Automatically advance while preserving per-source resolution behavior.
- [x] Keep played/current entries visible and provide previous/next controls.
- [x] Add queue looping and idle queue preview in the mini player.

## 2. YouTube browsing depth

- [x] Add a video details view with description, metadata, and actions.
- [x] Add channel pages with channel metadata and recent videos.
- [x] Add paginated top-level comments and replies.
- [x] Handle unavailable, age-restricted, and members-only content clearly.

## 3. Offline library

- [x] Download selected video/audio quality with captions and metadata.
- [x] Show download progress, failures, retries, and cancellation.
- [x] Add an offline library with search, playback, storage usage, and deletion.
- [x] Detect moved or missing files without corrupting library state.

## 4. Playlists

- [x] Create and manage local playlists containing online and offline media.
- [x] Browse YouTube playlists and play them as queues.
- [x] After OAuth, create, edit, and delete account playlists.

## 5. YouTube account

- [x] Add Google desktop OAuth with PKCE and the smallest useful scopes.
- [x] Store refresh tokens in the system keyring rather than the config file.
- [x] Add account switching and explicit sign-out/revocation.
- [x] Browse subscriptions, liked videos, history where available, and playlists.

## 6. Homepage

- [x] Add Continue Watching and offline/download sections.
- [x] Add subscription activity and API-provided account recommendations.
- [x] Provide a useful signed-out homepage based on local history and channels.
- [x] Clearly distinguish local ranking from YouTube-provided recommendations.

## 7. Synchronized playback

- [x] Define room protocol, host authority, and participant permissions.
- [x] Synchronize media identity, play/pause, seeks, and playback position.
- [x] Correct clock drift smoothly and recover after reconnects.
- [x] Add room creation/join UI and an independently deployable room server.
