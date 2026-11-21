---
name: twitch-spike-clipper
description: Detect Twitch VOD chat spikes and export framed, captioned vertical/horizontal MP4 clips.
---

# Twitch Spike Clipper

## Workflow
1. Extract numeric VOD ID from the user's Twitch URL (`https://www.twitch.tv/videos/<id>`).
2. Download chat replay JSON only via `TwitchDownloaderCLI chatdownload --id <id> -o outputs/<id>/chat_<id>.json`.
3. Run `analyze_chat_spikes()` to generate `outputs/<id>/manifest.json`.
4. Present top ranked timestamps, Z-scores, and sample chat reactions before downloading video segments.
5. Download only approved `[start_sec, end_sec]` windows via `TwitchDownloaderCLI videodownload` and render via `FFmpeg`.
6. Inspect the generated 4x2 contact sheet (`*_contact.png`) before final delivery.