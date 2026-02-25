import os
import sys
import re
import argparse
import threading
from playwright.sync_api import sync_playwright

# Local imports
import downloader
import merger

def strip_range_param(url: str) -> str:
    """Remove everything from '&range=' to the end of the URL"""
    range_start = url.find("&range=")
    if range_start != -1:
        return url[:range_start]
    return url

def parse_url_for_selection(url: str):
    """Parse metadata for size/itag comparison"""
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
    if not sources: return None
    with_clen = [s for s in sources if s["clen"] > 0]
    if with_clen:
        return max(with_clen, key=lambda x: x["clen"])
    return max(sources, key=lambda x: int(x["itag"]) if x["itag"].isdigit() else 0)

def main():
    parser = argparse.ArgumentParser(description="Google Drive Media Capture")
    parser.add_argument("url", help="Google Drive / Web view URL")
    parser.add_argument("-i", "--interactive", action="store_true", help="Ask for download options")
    args = parser.parse_args()

    captured_urls = set()
    user_data_dir = os.path.expanduser("~/.config/google-chrome")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response):
            if response.status not in (200, 206): return
            ct = response.headers.get("content-type", "").lower()
            u = response.url.lower()
            if "videoplayback" in u or ct.startswith(("video/", "audio/")):
                if response.url not in captured_urls:
                    captured_urls.add(response.url)
                    print(f"→ captured: {response.url[:70]}...")

        page.on("response", on_response)
        print(f"Launching Browser to: {args.url}")
        page.goto(args.url)

        print("\nBrowser is open.")
        print(" → Play the video and wait for captures...")
        print(" → Press ENTER when ready.")
        input("\nPress Enter when ready → ")

        browser_user_agent = page.evaluate("navigator.userAgent")
        browser_cookies = context.cookies()
        
        cookie_file = "cookies.txt"
        with open(cookie_file, "w") as f:
            f.write("# Netscape HTTP Cookie File\n\n")
            for c in browser_cookies:
                domain = c['domain']
                host_only = "FALSE" if domain.startswith(".") else "TRUE"
                expires = int(c.get('expires', 2147483647))
                secure = "TRUE" if c['secure'] else "FALSE"
                f.write(f"{domain}\t{host_only}\t{c['path']}\t{secure}\t{expires}\t{c['name']}\t{c['value']}\n")
        
        page_title = page.title()
        safe_title = re.sub(r'[\\/*?:"<>|]', "", page_title)
        safe_title = safe_title.replace(" - Google Drive", "").replace("Google Drive -", "").replace("Google Drive", "").strip()
        if not safe_title: safe_title = "output"

        context.close()

    if not captured_urls:
        print("No urls captured.")
        return

    parsed_streams = [parse_url_for_selection(u) for u in captured_urls]
    audio_streams = [s for s in parsed_streams if s["mime"].startswith("audio/") or (s["itag"].isdigit() and int(s["itag"]) in {139, 140, 141, 171, 172, 249, 250, 251})]
    video_streams = [s for s in parsed_streams if s["mime"].startswith("video/") and not (s["itag"].isdigit() and int(s["itag"]) in {139, 140, 141, 171, 172, 249, 250, 251})]

    best_video = get_best(video_streams)
    best_audio = get_best(audio_streams)

    clean_video_url = strip_range_param(best_video["original_url"]) if best_video else None
    clean_audio_url = strip_range_param(best_audio["original_url"]) if best_audio else None

    # Handle Logic
    dl_video = False
    dl_audio = False
    merge_needed = False

    if args.interactive:
        print("\n--- INTERACTIVE MODE ---")
        if best_video: print(f"🎥 Video found: {best_video['itag']} ({best_video['mime']})")
        if best_audio: print(f"🔊 Audio found: {best_audio['itag']} ({best_audio['mime']})")
        
        choice = input("\nDownload: (v)video, (a)audio, (b)both/merge [default: b]: ").lower() or 'b'
        if choice == 'v' and best_video: dl_video = True
        elif choice == 'a' and best_audio: dl_audio = True
        elif choice == 'b':
            dl_video = bool(best_video)
            dl_audio = bool(best_audio)
            merge_needed = dl_video and dl_audio
    else:
        # Default behavior: Auto download everything available
        dl_video = bool(best_video)
        dl_audio = bool(best_audio)
        merge_needed = dl_video and dl_audio
        print(f"\n--- AUTO-DOWNLOAD MODE ({'Both' if merge_needed else 'Single Stream'}) ---")

    video_tmp = "video_tmp.mp4"
    audio_tmp = "audio_tmp.m4a"
    results = [True, True] # Success tracker for video (0) and audio (1)

    def download_worker(idx, label, url, output_file, user_agent, cookie_file, bar_pos):
        results[idx] = downloader.fast_download(label, url, output_file, user_agent, cookie_file, bar_pos=bar_pos)

    threads = []
    if dl_video:
        t = threading.Thread(target=download_worker, args=(0, "VIDEO", clean_video_url, video_tmp, browser_user_agent, cookie_file, 0))
        threads.append(t)
    if dl_audio:
        t = threading.Thread(target=download_worker, args=(1, "AUDIO", clean_audio_url, audio_tmp, browser_user_agent, cookie_file, 1))
        threads.append(t)

    for t in threads: t.start()
    for t in threads: t.join()
    
    # Ensure terminal returns to normal after multiple bars
    print("\n" * (len(threads))) 

    # Final Step: Merge or Rename
    if merge_needed:
        if results[0] and results[1]:
            merger.merge_video_audio(video_tmp, audio_tmp, f"{safe_title}.mp4")
        else:
            print("Download failed, skipping merge.")
    elif dl_video and results[0]:
        os.rename(video_tmp, f"{safe_title}.mp4")
        print(f"✅ Saved as {safe_title}.mp4")
    elif dl_audio and results[1]:
        os.rename(audio_tmp, f"{safe_title}.m4a")
        print(f"✅ Saved as {safe_title}.m4a")

if __name__ == "__main__":
    main()
