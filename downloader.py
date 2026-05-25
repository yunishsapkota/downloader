import os
import shutil
import threading
import urllib.request
from typing import Optional, Set

from tqdm import tqdm

import cookie
import merger

AUDIO_ITAGS = {139, 140, 141, 171, 172, 249, 250, 251}


def strip_range_param(url: str) -> str:
    """Remove everything from '&range=' to the end of the URL."""
    range_start = url.find("&range=")
    if range_start != -1:
        return url[:range_start]
    return url


def parse_url_for_selection(url: str) -> dict:
    """Parse metadata for size/itag comparison."""
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


def get_best(sources: list) -> Optional[dict]:
    """Pick the best stream by clen, then itag."""
    if not sources:
        return None
    with_clen = [s for s in sources if s["clen"] > 0]
    if with_clen:
        return max(with_clen, key=lambda x: x["clen"])
    return max(sources, key=lambda x: int(x["itag"]) if x["itag"].isdigit() else 0)


def _is_audio_stream(stream: dict) -> bool:
    if stream["mime"].startswith("audio/"):
        return True
    if stream["itag"].isdigit() and int(stream["itag"]) in AUDIO_ITAGS:
        return True
    return False


def _is_video_stream(stream: dict) -> bool:
    return stream["mime"].startswith("video/") and not _is_audio_stream(stream)


def build_file_paths(safe_title: str, temp_dir: str, output_dir: str) -> tuple[str, str, str, str]:
    base = safe_title or "output"
    video_tmp = os.path.join(temp_dir, f"{base}_video_tmp.mp4")
    audio_tmp = os.path.join(temp_dir, f"{base}_audio_tmp.m4a")
    output_mp4 = os.path.join(output_dir, f"{base}.mp4")
    output_m4a = os.path.join(output_dir, f"{base}.m4a")
    return video_tmp, audio_tmp, output_mp4, output_m4a


def _resolve_download_plan(
    best_video: Optional[dict],
    best_audio: Optional[dict],
    interactive: bool,
) -> tuple[bool, bool, bool]:
    """Return (dl_video, dl_audio, merge_needed)."""
    if interactive:
        print("\n--- INTERACTIVE MODE ---")
        if best_video:
            print(f"🎥 Video found: {best_video['itag']} ({best_video['mime']})")
        if best_audio:
            print(f"🔊 Audio found: {best_audio['itag']} ({best_audio['mime']})")

        choice = input("\nDownload: (v)video, (a)audio, (b)both/merge [default: b]: ").lower() or "b"
        if choice == "v" and best_video:
            return True, False, False
        if choice == "a" and best_audio:
            return False, True, False
        if choice == "b":
            dl_video = bool(best_video)
            dl_audio = bool(best_audio)
            return dl_video, dl_audio, dl_video and dl_audio
        return False, False, False

    dl_video = bool(best_video)
    dl_audio = bool(best_audio)
    merge_needed = dl_video and dl_audio
    print(f"\n--- AUTO-DOWNLOAD MODE ({'Both' if merge_needed else 'Single Stream'}) ---")
    return dl_video, dl_audio, merge_needed


def fast_download(
    label,
    url,
    output_file,
    user_agent,
    cookie_file: Optional[str] = None,
    num_parts=16,
    bar_pos=0,
):
    """
    Parallel range-request downloader with optional cookies and tqdm progress bar.
    """
    opener = cookie.build_opener(user_agent, cookie_file)

    tqdm.write(f"⬇  {label}: probing file size...")

    try:
        with opener.open(url, timeout=15) as r:
            total = int(r.headers.get("Content-Length", 1))
    except Exception as e:
        tqdm.write(f"   {label} probe failed ({e}), trying single connection...")
        total = 0

    if total <= 1:
        tqdm.write(f"   Falling back to single-connection download for {label}...")
        try:
            with opener.open(url, timeout=60) as r, open(output_file, "wb") as f:
                shutil.copyfileobj(r, f)
            tqdm.write(f"   ✓ {label} done.")
            return True
        except Exception as e:
            tqdm.write(f"   ✗ {label} failed: {e}")
            return False

    chunk = (total + num_parts - 1) // num_parts
    ranges = []
    for i in range(num_parts):
        start = i * chunk
        end = min(start + chunk - 1, total - 1)
        if start <= total - 1:
            ranges.append((i, start, end))

    actual_parts = len(ranges)
    temp_files = [f"{output_file}.part{i}" for i in range(actual_parts)]
    errors = [None] * actual_parts
    lock = threading.Lock()

    pbar = tqdm(total=actual_parts, desc=f"   {label}", unit="part", position=bar_pos, leave=True)

    def fetch_part(idx, start, end):
        try:
            req = urllib.request.Request(url)
            req.add_header("Range", f"bytes={start}-{end}")
            with opener.open(req, timeout=60) as r, open(temp_files[idx], "wb") as f:
                shutil.copyfileobj(r, f)
            with lock:
                pbar.update(1)
        except Exception as e:
            errors[idx] = e

    threads = [threading.Thread(target=fetch_part, args=(i, s, e)) for i, s, e in ranges]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pbar.close()

    if any(errors):
        tqdm.write(f"   ✗ {label}: {sum(1 for e in errors if e)} part(s) failed.")
        return False

    tqdm.write(f"   Assembling {label}...")
    with open(output_file, "wb") as out:
        for tf in temp_files:
            if os.path.exists(tf):
                with open(tf, "rb") as part:
                    shutil.copyfileobj(part, out)
                os.remove(tf)

    tqdm.write(f"   ✓ {label} saved → {output_file}")
    return True


def download_captured_streams(
    captured_urls: Set[str],
    *,
    interactive: bool,
    temp_dir: str,
    output_dir: str,
    cookie_file: Optional[str],
    browser_user_agent: str,
    safe_title: str,
) -> bool:
    """Select best streams, download in parallel, merge or save output."""
    if not captured_urls:
        print("No urls captured.")
        return False

    parsed_streams = [parse_url_for_selection(u) for u in captured_urls]
    audio_streams = [s for s in parsed_streams if _is_audio_stream(s)]
    video_streams = [s for s in parsed_streams if _is_video_stream(s)]

    best_video = get_best(video_streams)
    best_audio = get_best(audio_streams)

    clean_video_url = strip_range_param(best_video["original_url"]) if best_video else None
    clean_audio_url = strip_range_param(best_audio["original_url"]) if best_audio else None

    dl_video, dl_audio, merge_needed = _resolve_download_plan(
        best_video, best_audio, interactive
    )

    if not dl_video and not dl_audio:
        print("Nothing selected to download.")
        return False

    video_tmp, audio_tmp, output_mp4, output_m4a = build_file_paths(
        safe_title, temp_dir, output_dir
    )
    results = [True, True]

    def download_worker(idx, label, url, output_file, user_agent, cookie_path, bar_pos):
        results[idx] = fast_download(
            label, url, output_file, user_agent, cookie_path, bar_pos=bar_pos
        )

    threads = []
    if dl_video:
        threads.append(
            threading.Thread(
                target=download_worker,
                args=(0, "VIDEO", clean_video_url, video_tmp, browser_user_agent, cookie_file, 0),
            )
        )
    if dl_audio:
        threads.append(
            threading.Thread(
                target=download_worker,
                args=(1, "AUDIO", clean_audio_url, audio_tmp, browser_user_agent, cookie_file, 1),
            )
        )

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print("\n" * len(threads))

    if merge_needed:
        if results[0] and results[1]:
            merger.merge_video_audio(video_tmp, audio_tmp, output_mp4)
            return True
        print("Download failed, skipping merge.")
        return False
    if dl_video and results[0]:
        shutil.move(video_tmp, output_mp4)
        print(f"✅ Saved as {output_mp4}")
        return True
    if dl_audio and results[1]:
        shutil.move(audio_tmp, output_m4a)
        print(f"✅ Saved as {output_m4a}")
        return True
    return False
