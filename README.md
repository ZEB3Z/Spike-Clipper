# twitch-spike-clipper

Turn multi-hour Twitch VODs into vertical or widescreen highlight clips by reading chat velocity spikes first - before downloading a single megabyte of video.

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-Local%20UI-009688?style=flat-square&logo=fastapi&logoColor=white)
![FFmpeg](https://img.shields.io/badge/FFmpeg-Rendered-007808?style=flat-square&logo=ffmpeg&logoColor=white)
![Whisper](https://img.shields.io/badge/faster--whisper-Local%20Captions-9147FF?style=flat-square)

```text
Twitch VOD URL
      |
      v
[TwitchDownloaderCLI] --> chat_<id>.json (seconds, ~2 MB)
      |
      v
[Spike Engine]        --> Rolling 90s Z-score + emote weights + -7s latency shift
      |
      v
[Browser UI]          --> Interactive velocity graph + freeform webcam crop box
      |
      v
[FFmpeg + Whisper]    --> 9:16 split / 16:9 MP4 + .ass karaoke captions + 4x2 QC sheet
```

## Why I Built This

Most auto-clipping tools either lock basic exports behind a $20/month subscription or waste bandwidth downloading an entire 30 GB VOD just to scan audio levels. On Twitch, chat is a better highlight detector anyway. When something funny or ridiculous happens on stream, chat explodes with `KEKW`, `OMEGALUL`, `POG`, or `CLIP IT` roughly 6 to 10 seconds later.

`twitch-spike-clipper` pulls the chat log first, finds the statistical outliers, shifts the timestamps backward to catch the actual setup, and downloads only the 25-to-35 second slices you decide to export. Everything runs locally on your machine with zero API keys or watermarks.

## Features

- **Chat-First Discovery:** Scans hours of chat history in a couple of seconds using a rolling 90-second baseline so quiet Just Chatting segments and chaotic boss fights both scale fairly.
- **Freeform 9:16 Reframing:** Blind center-crops ruin gaming streams by cutting off half the HUD and the streamer's face. Grab a sample frame from the VOD, drag the yellow webcam box anywhere on screen, pull any edge or corner handle to size it, and stack it cleanly above centered gameplay (`1080x1920`).
- **Word-by-Word Karaoke Captions:** Runs `faster-whisper` locally on CPU (`int8`) to generate styled `.ass` subtitles that highlight each spoken word in the UI-safe center zone so TikTok, Reels, and Shorts buttons never cover the text.
- **Broadcast Loudness & QC Sheets:** Levels sudden streamer mic screams to `-14 LUFS` via FFmpeg `loudnorm` and generates an 8-frame (`4x2`) contact sheet alongside every MP4 so you can verify framing at a glance.
- **Persistent Local Cache:** Saves parsed manifests, preview frames, and rendered variants inside `outputs/<vod_id>/` with a dropdown to reload past VODs instantaneously.

## Prerequisites

- **Python 3.10+**
- **FFmpeg** in your system `PATH`
- **TwitchDownloaderCLI** in the project root folder or `PATH` (falls back to `yt-dlp` for video slices if installed)

## Setup & Run (Windows PowerShell)

1. **Clone the repository and enter the folder:**
   ```powershell
   git clone https://github.com/YOUR_USERNAME/twitch-spike-clipper.git
   cd twitch-spike-clipper
   ```

2. **Install FFmpeg and fetch `TwitchDownloaderCLI.exe`:**
   ```powershell
   winget install Gyan.FFmpeg --source winget --accept-package-agreements --accept-source-agreements

   $release = Invoke-RestMethod "https://api.github.com/repos/lay295/TwitchDownloader/releases/latest"
   $asset = $release.assets | Where-Object { $_.name -like "*CLI*Windows-x64.zip" } | Select-Object -First 1
   Invoke-WebRequest -Uri $asset.browser_download_url -OutFile "TwitchDownloaderCLI.zip"
   Expand-Archive -Path "TwitchDownloaderCLI.zip" -DestinationPath "." -Force
   Remove-Item "TwitchDownloaderCLI.zip"
   ```
   *(If you just installed FFmpeg, open a fresh terminal so Windows reloads your `PATH`.)*

3. **Install Python dependencies and start the local server:**
   ```powershell
   pip install -r requirements.txt
   python server.py
   ```

4. Open **`http://127.0.0.1:8000`** in your browser.

## Project Layout

```text
twitch-spike-clipper/
├── clipper_engine.py   # Chat parser, rolling Z-score detector, Whisper .ass & FFmpeg renderer
├── server.py           # FastAPI endpoints, VOD history cache & manifest persistence
├── requirements.txt    # Lightweight Python dependencies (FastAPI, Uvicorn, NumPy, faster-whisper)
├── SKILL.md            # Agent workflow instructions for CLI coding assistants
├── static/
│   └── index.html      # Single-file web dashboard with interactive spike chart & freeform cam box
└── outputs/            # Generated per-VOD folders (chat JSON, manifest.json, MP4s, contact sheets)
```

## Tuning Tips

- **Chat Delay Offset (`-7s` default):** Viewers usually take 6 to 10 seconds to react and hit Enter. If clips start slightly late, push this to `-9` or `-10`.
- **Spike Sensitivity / Z-Score (`2.2` default):** For slower chats (under 200 viewers), lower this to `1.6` - `1.8` so smaller bursts still register. For massive chats (10k+ viewers), bump it to `2.8` - `3.2` to filter out routine spam.
- **Export Formats:** Choose `9:16 Stacked Cam + Gameplay` for gaming VODs with a facecam, `9:16 Vertical Center Crop` for full-screen IRL streams, or `16:9 Original` for YouTube and X uploads.