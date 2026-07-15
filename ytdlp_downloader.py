"""
ytdlp_downloader.py — yt-dlp based download mode for the Drive/Classroom downloader.

Workflow:
  1. save_url_list()      — write extracted Drive URLs to a temp .txt file
  2. download_with_ytdlp() — read that file, call yt-dlp for each URL one-by-one
                             using the Playwright-exported cookie file for auth

Downloader priority:
  1. aria2c  (--downloader aria2c -x 16 -k 1M) — fastest, splits into 16 connections
  2. yt-dlp built-in -N 16 (concurrent fragments) — fallback if aria2c not found
"""

import os
import shutil
import subprocess
import sys
from typing import List

from filter import DriveItem

# Always grab the best available quality and mux into MP4
YTDLP_FORMAT = "bestvideo+bestaudio/best"
YTDLP_MERGE_FORMAT = "mp4"
URL_LIST_FILENAME = "ytdlp_urls.txt"

# aria2c settings — 16 connections, 1 MB chunk size
ARIA2C_CONNECTIONS = 16
ARIA2C_CHUNK_SIZE = "20M"

# yt-dlp built-in fallback — concurrent fragment count
YTDLP_CONCURRENT_FRAGMENTS = 16


def _aria2c_available() -> bool:
    """Return True if aria2c is on PATH."""
    return shutil.which("aria2c") is not None


def _build_downloader_args(use_aria2c: bool, fragments: int) -> list:
    """Return yt-dlp downloader flags based on user preferences."""
    if use_aria2c:
        if _aria2c_available():
            print(
                "→ Downloader: aria2c  "
                f"(-x {ARIA2C_CONNECTIONS} -k {ARIA2C_CHUNK_SIZE})"
            )
            return [
                "--downloader",
                "aria2c",
                "--downloader-args",
                f"aria2c:-x {ARIA2C_CONNECTIONS} -k {ARIA2C_CHUNK_SIZE}",
            ]
        else:
            print(
                "⚠  aria2c requested but not found on PATH. Falling back to default yt-dlp downloader."
            )
            return []
    elif fragments > 0:
        print(f"→ Downloader: yt-dlp built-in concurrent fragments (-N {fragments})")
        return ["--concurrent-fragments", str(fragments)]
    else:
        # Default behavior: single connection
        return []


def save_url_list(items: List[DriveItem], temp_dir: str) -> str:
    """Write Drive video URLs to a temp file (one URL per line).

    Returns the absolute path to the written file.
    """
    url_list_path = os.path.join(temp_dir, URL_LIST_FILENAME)
    with open(url_list_path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(item["url"] + "\n")
    print(f"→ URL list written to: {url_list_path}  ({len(items)} link(s))")
    return url_list_path


def download_with_ytdlp(
    url_list_file: str,
    cookie_file: str,
    output_dir: str,
    use_aria2c: bool = False,
    fragments: int = 0,
) -> None:
    """Download each URL from *url_list_file* with yt-dlp, one at a time.

    Args:
        url_list_file: Path to the text file produced by save_url_list().
        cookie_file:   Path to the Netscape cookies.txt exported by cookie.save_cookies().
        output_dir:    Directory where finished videos are saved.
        use_aria2c:    Whether to use aria2c as the downloader.
        fragments:     Number of concurrent fragments for yt-dlp built-in.
    """
    with open(url_list_file, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    total = len(urls)
    if not urls:
        print("No URLs found in list — nothing to download.")
        return

    print(f"\nStarting yt-dlp: {total} video(s) queued")

    # Detect downloader once for the whole batch
    downloader_args = _build_downloader_args(use_aria2c, fragments)

    output_template = os.path.join(output_dir, "%(title)s.%(ext)s")

    succeeded = 0
    failed = 0

    for i, url in enumerate(urls, start=1):
        print(f"\n{'=' * 60}")
        print(f"[{i}/{total}] {url}")
        print(f"{'=' * 60}")

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--format",
            YTDLP_FORMAT,
            "--merge-output-format",
            YTDLP_MERGE_FORMAT,
            "--output",
            output_template,
            "--progress",
            "--no-warnings",
        ]

        # Inject cookies when the file exists
        if cookie_file and os.path.exists(cookie_file):
            cmd += ["--cookies", cookie_file]
        else:
            print(
                "⚠  Cookie file not found — private videos may fail to download.\n"
                "   Run --setup first to export browser cookies."
            )

        # Append downloader flags (aria2c or built-in -N 16)
        cmd += downloader_args

        cmd.append(url)

        try:
            result = subprocess.run(cmd, check=False)
            if result.returncode == 0:
                print(f"✅ [{i}/{total}] Done.")
                succeeded += 1
            else:
                print(f"❌ [{i}/{total}] yt-dlp exited with code {result.returncode}.")
                failed += 1
        except FileNotFoundError:
            print(
                "❌ yt-dlp not found on PATH.\n"
                "   Install it with:  pip install yt-dlp"
            )
            sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"yt-dlp complete — {succeeded} succeeded, {failed} failed  (total: {total})")
    print(f"{'=' * 60}\n")
