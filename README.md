# yt-hls-server

A Flask-based server that converts YouTube videos into multi-quality HLS streams with an embeddable player.

## Features

- Convert YouTube videos to HLS format with multiple quality levels (480p, 720p, 1080p)
- Embeddable video player with adaptive streaming
- Channel subscription system for automatic video processing
- Google Cloud Storage integration for stream hosting

## Setup

1. Install dependencies:

```bash
pip install flask yt-dlp google-cloud-storage feedparser
```

2. Set up Google Cloud Storage:
   - Create a bucket
   - Place your service account credentials in `~/.gcloud/connecthack.json`

3. Run the server:

```bash
python main.py
```

4. Access the web interface at `http://localhost:5000`.

## Usage

- Convert video: Visit `/` and enter a YouTube URL
- Watch video: `/player?video=VIDEO_ID`
- Embed video: `/player?video=VIDEO_ID&embed=1`
- Subscribe to channel: `/subscribe`

## Environment

- Python 3.7+
- FFmpeg (required for HLS conversion)
- Google Cloud Storage account
