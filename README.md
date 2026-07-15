# Google Drive & Classroom Video Downloader

A modular Python tool to capture and download high-quality video and audio streams from Google Drive and Google Classroom view pages.

## Features
- **yt-dlp Integration**: High-performance downloading for Google Classroom URLs, with optional `aria2c` support for even faster concurrent downloads.
- **Parallel Downloading**: Bypasses rate limits using multiple simultaneous connections (via `aria2c` or built-in fragment downloading).
- **Auto-Selection**: Automatically finds the best quality video and audio streams.
- **Secure Authentication**: Extracts cookies from your browser session to bypass 403 Forbidden errors, even for private or restricted videos.
- **Modular Design**: Separated browser automation, stream extraction, yt-dlp integration, and ffmpeg merging.

## Project Structure
- `source.py`: Main entry point. Handles CLI arguments, browser interaction, and mode selection.
- `lib/runner.py`: Orchestrates different modes (setup, single video, classroom, yt-dlp).
- `lib/downloader.py`: Handles legacy parallel range-request downloading.
- `lib/merger.py`: Uses FFmpeg to combine video and audio streams for legacy mode.
- `lib/ytdlp_downloader.py`: Handles yt-dlp based downloading for Google Classroom.

## Prerequisites
1. **Python 3.7+**
2. **Playwright & yt-dlp**:
   ```bash
   pip install playwright tqdm yt-dlp
   playwright install chromium
   ```
3. **FFmpeg**: Required for merging video and audio. Ensure it's in your system PATH.
4. **aria2c (Optional but Recommended)**: Required for the fastest `yt-dlp` download mode (`-a` flag). Ensure it's in your system PATH.

## Usage

### First-Time Setup
Launch a persistent browser session so you can sign in to your Google account and clear any security warnings.
```bash
python source.py --setup
```

### Google Classroom URLs
By default, Classroom URLs will use the `yt-dlp` downloader.
```bash
# Fastest option: Use aria2c with yt-dlp (requires aria2c installed)
python source.py -a "https://classroom.google.com/c/.../p/..."

# Alternative: Use yt-dlp's built-in concurrent fragments (e.g., 16 connections)
python source.py -n 16 "https://classroom.google.com/c/.../p/..."

# Legacy Mode: Use the custom built-in stream-capture instead of yt-dlp
python source.py --custom "https://classroom.google.com/c/.../p/..."
```

### Single Google Drive Videos
Run the script with a Drive URL. It will automatically capture streams and download/merge them.
```bash
python source.py "https://drive.google.com/file/d/.../view"
```

### Interactive Mode
Use the `-i` flag to choose specific streams or download types.
```bash
python source.py -i "https://drive.google.com/file/d/.../view"
```

## How it Works
1. Launches a Chromium browser using a persistent profile.
2. Navigates to the Google Drive/Classroom link.
3. Automatically exports your session cookies to allow downloading of restricted videos.
4. Depending on the mode:
   - **Classroom/yt-dlp mode**: Extracts the video URLs and hands them off to `yt-dlp` (and optionally `aria2c`) to perform fast concurrent downloading.
   - **Single/Custom mode**: Monitors network traffic to find raw stream URLs, sanitizes them, and downloads chunks in parallel.
5. Final videos are merged and placed in your output directory.

## Disclaimer

This project is provided **"as is"** without any warranty. The author is **not responsible** for any damage, loss, misuse, illegal activity, or consequences — including violation of terms of service — that may arise from using this tool.

You use it **at your own risk** and are solely responsible for complying with all applicable laws and platform policies.
