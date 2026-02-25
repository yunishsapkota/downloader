# Google Drive Media Capture

A modular Python tool to capture and download high-quality video and audio streams from Google Drive/Web view pages.

## Features
- **Parallel Downloading**: Bypasses rate limits by using multiple simultaneous connections.
- **Auto-Selection**: Automatically finds the best quality video and audio streams.
- **Secure Authentication**: Extracts cookies from your browser session to bypass 403 Forbidden errors.
- **Modular Design**: Separated downloader, merger, and extraction logic.

## Project Structure
- `source.py`: Main entry point. Handles browser interaction and stream discovery.
- `downloader.py`: Handles parallel range-request downloading.
- `merger.py`: Uses FFmpeg to combine video and audio streams.
- `cookies.txt`: Generated file containing session cookies (do not share this).

## Prerequisites
1. **Python 3.7+**
2. **Playwright**: For browser automation.
   ```bash
   pip install playwright
   playwright install chromium
   ```
3. **FFmpeg**: Required for merging video and audio. Ensure it's in your system PATH.

## Usage

### Default (Auto-Download)
Run the script with a URL. It will capture the streams and automatically download/merge them using the page title as the filename.
```bash
python source.py "https://drive.google.com/file/d/.../view"
```

### Interactive Mode
Use the `-i` flag to choose specific streams or download types.
```bash
python source.py -i "https://drive.google.com/file/d/.../view"
```

## How it Works
1. Launches a Chromium browser with your profile.
2. Monitors network traffic to find "videoplayback" URLs.
3. Sanitizes the URLs (removes range parameters).
4. Saves session cookies to `cookies.txt`.
5. Downloads streams in parallel chunks.
6. Merges using FFmpeg.
