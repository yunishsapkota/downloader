import os
import sys
import re

from playwright.sync_api import sync_playwright


def strip_range_param(url: str) -> str:
    """Remove everything from '&range=' to the end of the URL"""
    # Find the starting position of '&range='
    range_start = url.find("&range=")
    if range_start != -1:
        # Slice the URL up to that point
        return url[:range_start]
    else:
        # If no '&range=', return the original URL
        return url


def parse_url_for_selection(url: str):
    """Parse metadata for size/itag comparison (keep original URL)"""
    # Simple string parsing since we avoid urlparse for encoding issues
    params = {}
    query = url.split("?")[1] if "?" in url else ""
    for param in query.split("&"):
        if "=" in param:
            key, value = param.split("=", 1)
            params[key] = value

    itag = params.get("itag", "")
    clen_str = params.get("clen", "0")
    clen = int(clen_str) if clen_str.isdigit() else 0
    mime = params.get("mime", "").split(";")[0]
    quality = params.get("quality", "")

    return {
        "original_url": url,
        "itag": itag,
        "clen": clen,
        "mime": mime,
        "quality": quality,
    }


def get_best(sources):
    """Pick the best stream by clen → itag"""
    if not sources:
        return None

    # Prefer real content-length when available
    with_clen = [s for s in sources if s["clen"] > 0]
    if with_clen:
        return max(with_clen, key=lambda x: x["clen"])

    # Fallback: highest itag number
    return max(sources, key=lambda x: int(x["itag"]) if x["itag"].isdigit() else 0)


def main(url):
    captured_urls = set()  # temporary storage of raw URLs

    user_data_dir = os.path.expanduser("~/.config/google-chrome")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response):
            if response.status not in (200, 206):
                return
            ct = response.headers.get("content-type", "").lower()
            u = response.url.lower()
            if "videoplayback" in u or ct.startswith(("video/", "audio/")):
                if response.url not in captured_urls:
                    captured_urls.add(response.url)
                    print(f"→ captured: {response.url}")

        page.on("response", on_response)

        print("Launching Chrome with your profile...")
        page.goto(
            url
        )  # Removed wait_until to prevent timeout issues; browser opens immediately

        print("\nBrowser is open.")
        print(" → If needed, sign in or handle any prompts")
        print(" → Play the video (or let it auto-play)")
        print(
            " → Wait 15–30 seconds for different qualities to appear (switch resolutions if possible)"
        )
        print(" → Then return here and press ENTER")

        input("\nPress Enter when ready → ")

        # Capture cookies and user-agent to bypass 403 Forbidden later
        browser_user_agent = page.evaluate("navigator.userAgent")
        browser_cookies = context.cookies()
        cookie_string = "; ".join([f"{c['name']}={c['value']}" for c in browser_cookies])

        page_title = page.title()
        safe_title = re.sub(r'[\\/*?:"<>|]', "", page_title)
        safe_title = safe_title.replace(" - Google Drive", "").replace("Google Drive -", "").replace("Google Drive", "").strip()
        if not safe_title:
            safe_title = "output"

        context.close()

    # ────────────────────────────────────────────────
    print("\n" + "═" * 85)
    print("ALL CAPTURED URLS (with original parameters including range)")
    print("═" * 85)
    sorted_urls = sorted(captured_urls, key=len, reverse=True)
    for u in sorted_urls:
        print(u)

    # Parse for selection (but keep original URLs)
    parsed_streams = [parse_url_for_selection(u) for u in sorted_urls]

    # Classify video vs audio
    audio_streams = [
        s
        for s in parsed_streams
        if s["mime"].startswith("audio/")
        or (
            s["itag"].isdigit()
            and int(s["itag"]) in {139, 140, 141, 171, 172, 249, 250, 251}
        )
    ]

    video_streams = [
        s
        for s in parsed_streams
        if s["mime"].startswith("video/")
        and not (
            s["itag"].isdigit()
            and int(s["itag"]) in {139, 140, 141, 171, 172, 249, 250, 251}
        )
    ]

    best_video = get_best(video_streams)
    best_audio = get_best(audio_streams)

    print("\n" + "═" * 85)
    print(
        "RECOMMENDED – BEST QUALITY (showing both original and stripped for comparison)"
    )
    print("═" * 85)

    if best_video:
        clean_video_url = strip_range_param(best_video["original_url"])
        size_mb = (
            best_video["clen"] / (1024 * 1024) if best_video["clen"] > 0 else "unknown"
        )
        print("🎥 BEST VIDEO:")
        print("   Original URL (with range):")
        print(best_video["original_url"])
        print("   Stripped URL (full access):")
        print(clean_video_url)
        print(
            f"   itag: {best_video['itag']}  |  size: {size_mb:,.1f} MB  |  {best_video['mime']}"
        )
    else:
        print("No suitable video stream found")

    if best_audio:
        clean_audio_url = strip_range_param(best_audio["original_url"])
        size_mb = (
            best_audio["clen"] / (1024 * 1024) if best_audio["clen"] > 0 else "unknown"
        )
        print("\n🔊 BEST AUDIO:")
        print("   Original URL (with range):")
        print(best_audio["original_url"])
        print("   Stripped URL (full access):")
        print(clean_audio_url)
        print(
            f"   itag: {best_audio['itag']}  |  size: {size_mb:,.1f} MB  |  {best_audio['mime']}"
        )
    else:
        print("\nNo separate audio stream found")

    print("\nUsage example (full file download):")
    print(f'  ffmpeg -i "stripped_video_url" -i "stripped_audio_url" -c copy "{safe_title}.mp4"')
    print("  # or wget/curl directly on the stripped URLs above")

    # --- Auto-Download Trigger ---

    def fast_download(label, url, output_file, user_agent, cookie_str, num_parts=16):
        """
        Parallel range-request downloader.
        Opens `num_parts` simultaneous HTTP connections each fetching a different
        byte range — bypasses Google's ~30 KiB/s per-connection rate limit.
        Parts are written to temp files then concatenated (no large RAM usage).
        """
        import urllib.request
        import threading

        headers = {"User-Agent": user_agent, "Cookie": cookie_str}
        print(f"\n⬇  {label}: probing file size...")

        # HEAD request to get total size
        try:
            req = urllib.request.Request(url, headers=headers, method="HEAD")
            with urllib.request.urlopen(req, timeout=15) as r:
                total = int(r.headers.get("Content-Length", 0))
        except Exception as e:
            print(f"   HEAD failed ({e}), trying GET...")
            total = 0

        if total == 0:
            # Server doesn't support HEAD or Content-Length — single connection fallback
            print(f"   Falling back to single-connection download for {label}...")
            import shutil
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r, open(output_file, "wb") as f:
                shutil.copyfileobj(r, f)
            print(f"   ✓ {label} done.")
            return True

        size_mb = total / (1024 * 1024)
        print(f"   Size: {size_mb:.1f} MB  → splitting into {num_parts} parallel parts...")

        # Divide into num_parts byte ranges
        chunk = (total + num_parts - 1) // num_parts
        ranges = []
        for i in range(num_parts):
            start = i * chunk
            end = min(start + chunk - 1, total - 1)
            if start <= total - 1:
                ranges.append((i, start, end))

        actual_parts = len(ranges)
        temp_files = [f"{output_file}.part{i}" for i in range(actual_parts)]
        errors: list = [None] * actual_parts
        done_count = [0]
        lock = threading.Lock()

        def fetch_part(idx, start, end):
            h = dict(headers)
            h["Range"] = f"bytes={start}-{end}"
            try:
                req = urllib.request.Request(url, headers=h)
                import shutil
                with urllib.request.urlopen(req, timeout=60) as r, \
                        open(temp_files[idx], "wb") as f:
                    shutil.copyfileobj(r, f)
                with lock:
                    done_count[0] += 1
                    print(f"   [{label}] part {done_count[0]}/{actual_parts} done "
                          f"({(end - start + 1) / (1024*1024):.1f} MB)")
            except Exception as e:
                errors[idx] = e
                print(f"   [{label}] part {idx} ERROR: {e}")

        threads = [
            threading.Thread(target=fetch_part, args=(i, s, e))
            for i, s, e in ranges
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if any(errors):
            print(f"   ✗ {label}: {sum(1 for e in errors if e)} part(s) failed.")
            return False

        # Concatenate parts into final file
        print(f"   Assembling {label}...")
        with open(output_file, "wb") as out:
            for tf in temp_files:
                with open(tf, "rb") as part:
                    import shutil
                    shutil.copyfileobj(part, out)
                os.remove(tf)

        print(f"   ✓ {label} saved → {output_file}")
        return True

    if best_video and best_audio:
        ans = input("\nDo you want to automatically download and merge? (y/n): ")
        if ans.lower() == 'y':
            import subprocess
            import threading

            video_tmp = "video_tmp.mp4"
            audio_tmp = "audio_tmp.m4a"

            # Download video AND audio simultaneously (two parallel batch downloads)
            print("\nDownloading video + audio in parallel (16 connections each)...")
            results = [False, False]

            def dl_video():
                results[0] = fast_download("VIDEO", clean_video_url, video_tmp,
                                           browser_user_agent, cookie_string)

            def dl_audio():
                results[1] = fast_download("AUDIO", clean_audio_url, audio_tmp,
                                           browser_user_agent, cookie_string)

            t_video = threading.Thread(target=dl_video)
            t_audio = threading.Thread(target=dl_audio)
            t_video.start()
            t_audio.start()
            t_video.join()
            t_audio.join()

            if all(results):
                print("\n🔀 Merging with ffmpeg (-c copy, no re-encoding)...")
                subprocess.run([
                    "ffmpeg", "-y",
                    "-i", video_tmp,
                    "-i", audio_tmp,
                    "-c", "copy",
                    f"{safe_title}.mp4"
                ])
                for f in [video_tmp, audio_tmp]:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                print(f"\n✅ Done! Saved as {safe_title}.mp4")
            else:
                print("\n✗ Download failed — temp files left for inspection.")

    elif best_video or best_audio:
        ans = input("\nDo you want to automatically download the stream? (y/n): ")
        if ans.lower() == 'y':
            url_to_dl = clean_video_url if best_video else clean_audio_url
            ext = ".mp4" if best_video else ".m4a"
            fast_download("stream", url_to_dl, f"{safe_title}{ext}",
                          browser_user_agent, cookie_string)
            print(f"\n✅ Saved as {safe_title}{ext}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        print("Usage: python script.py https://drive.google.com/file/d/xxxxxxxx/view")
        # or uncomment default for testing:
        # main("https://drive.google.com/file/d/1byw3Sb_dxoMYRHYThhNGyGOdvwbBynmx/view")
