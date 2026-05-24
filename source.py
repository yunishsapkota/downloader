import os
import re
import sys
import time
import shutil
import argparse
import threading
from typing import List, Optional, Set, TypedDict

DEFAULT_CAPTURE_WAIT = 8
POST_LOAD_DELAY = 1
PLAY_CLICK_ATTEMPTS = 3
PLAY_CLICK_INTERVAL = 2
FILE_SKIP_COOLDOWN = 3
DEFAULT_TEMP_DIR = ".tmp"
DEFAULT_OUTPUT_DIR = "downloads"

from playwright.sync_api import Page, sync_playwright

import downloader
import merger

DRIVE_FILE_RE = re.compile(
    r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)", re.IGNORECASE
)
DRIVE_OPEN_RE = re.compile(
    r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)", re.IGNORECASE
)


class DriveItem(TypedDict):
    url: str
    title: str


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
    if not sources:
        return None
    with_clen = [s for s in sources if s["clen"] > 0]
    if with_clen:
        return max(with_clen, key=lambda x: x["clen"])
    return max(sources, key=lambda x: int(x["itag"]) if x["itag"].isdigit() else 0)


def is_classroom_url(url: str) -> bool:
    return "classroom.google.com" in url


def normalize_drive_url(url: str) -> Optional[str]:
    """Return a canonical Drive view URL, or None if not a Drive file link."""
    match = DRIVE_FILE_RE.search(url)
    if match:
        return f"https://drive.google.com/file/d/{match.group(1)}/view"
    match = DRIVE_OPEN_RE.search(url)
    if match:
        return f"https://drive.google.com/file/d/{match.group(1)}/view"
    return None


def extract_drive_items(page) -> List[DriveItem]:
    """Collect Drive links with topic/file titles from the current page."""
    raw_items = page.evaluate(
        """() => {
            const GENERIC = new Set([
                "open", "view", "download", "drive", "google drive", "more", "file",
            ]);
            const isGeneric = (text) => {
                const t = (text || "").trim().toLowerCase();
                return !t || GENERIC.has(t) || t.length < 2;
            };

            const toUrl = (href) => {
                if (!href) return null;
                const file = href.match(/drive\\.google\\.com\\/file\\/d\\/([a-zA-Z0-9_-]+)/i);
                if (file) return `https://drive.google.com/file/d/${file[1]}/view`;
                const open = href.match(/drive\\.google\\.com\\/open\\?id=([a-zA-Z0-9_-]+)/i);
                if (open) return `https://drive.google.com/file/d/${open[1]}/view`;
                return null;
            };

            const findTopicTitle = (node) => {
                let el = node;
                for (let i = 0; i < 15 && el; i++) {
                    const heading = el.querySelector('[role="heading"], h1, h2, h3, h4');
                    if (heading) {
                        const t = (heading.innerText || heading.textContent || "")
                            .trim()
                            .replace(/\\s+/g, " ");
                        if (!isGeneric(t)) return t;
                    }
                    el = el.parentElement;
                }
                return "";
            };

            const pickTitle = (linkTitle, topicTitle) => {
                const link = isGeneric(linkTitle) ? "" : linkTitle.trim();
                const topic = isGeneric(topicTitle) ? "" : topicTitle.trim();
                if (topic && link && link !== topic) return `${topic} — ${link}`;
                if (topic) return topic;
                if (link) return link;
                return "";
            };

            const found = new Map();

            const register = (href, linkTitle, topicTitle) => {
                const url = toUrl(href);
                if (!url) return;
                const title = pickTitle(linkTitle, topicTitle);
                const prev = found.get(url) || "";
                if (!prev || (title && title.length > prev.length)) {
                    found.set(url, title);
                }
            };

            document.querySelectorAll("a[href]").forEach((anchor) => {
                const href = anchor.href;
                if (!/drive\\.google\\.com/i.test(href)) return;
                const linkTitle = (
                    anchor.innerText ||
                    anchor.textContent ||
                    anchor.getAttribute("aria-label") ||
                    ""
                )
                    .trim()
                    .replace(/\\s+/g, " ");
                register(href, linkTitle, findTopicTitle(anchor));
            });

            document.querySelectorAll("iframe[src]").forEach((frame) => {
                register(frame.src, "", "");
            });

            const html = document.documentElement.innerHTML;
            for (const re of [
                /drive\\.google\\.com\\/file\\/d\\/([a-zA-Z0-9_-]+)/gi,
                /drive\\.google\\.com\\/open\\?id=([a-zA-Z0-9_-]+)/gi,
            ]) {
                let match;
                while ((match = re.exec(html)) !== null) {
                    const url = `https://drive.google.com/file/d/${match[1]}/view`;
                    if (!found.has(url)) found.set(url, "");
                }
            }

            return Array.from(found.entries()).map(([url, title]) => ({ url, title }));
        }"""
    )

    seen = set()
    items: List[DriveItem] = []
    for entry in raw_items:
        normalized = normalize_drive_url(entry["url"])
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        items.append({"url": normalized, "title": (entry.get("title") or "").strip()})
    return items


def display_name_for_item(item: DriveItem) -> str:
    return item["title"] or item["url"]


def save_cookies(context, cookie_file: str) -> None:
    with open(cookie_file, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write("# https://curl.haxx.se/rfc/cookie_spec.html\n\n")
        for c in context.cookies():
            domain = c.get("domain", "")
            if not domain:
                continue
            # Column 2: TRUE only when domain is prefixed with "." (subdomain cookie).
            include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path", "/") or "/"
            secure = "TRUE" if c.get("secure") else "FALSE"
            expires = c.get("expires", -1)
            expires = 0 if expires is None or expires < 0 else int(expires)
            name, value = c.get("name", ""), c.get("value", "")
            if "\t" in name or "\t" in value or "\n" in name or "\n" in value:
                continue
            f.write(
                f"{domain}\t{include_subdomains}\t{path}\t{secure}\t{expires}\t"
                f"\t{name}\t{value}\n"
            )


def get_safe_title(page) -> str:
    page_title = page.title()
    safe_title = re.sub(r'[\\/*?:"<>|]', "", page_title)
    safe_title = (
        safe_title.replace(" - Google Drive", "")
        .replace("Google Drive -", "")
        .replace("Google Drive", "")
        .strip()
    )
    return safe_title or "output"


def attach_capture_handler(page, captured_urls: Set[str]) -> None:
    def on_response(response):
        if response.status not in (200, 206):
            return
        ct = response.headers.get("content-type", "").lower()
        u = response.url.lower()
        if not ("videoplayback" in u or ct.startswith(("video/", "audio/"))):
            return
        if response.url not in captured_urls:
            captured_urls.add(response.url)
            print(f"→ captured: {response.url[:70]}...")

    page.on("response", on_response)


PLAY_SELECTORS = (
    '[aria-label="Play"]',
    '[aria-label*="Play" i]',
    '[data-tooltip="Play"]',
    '[aria-label="Play video"]',
    'div[role="button"][aria-label*="Play" i]',
)


def _click_play(page: Page) -> None:
    for frame in page.frames:
        try:
            btn = frame.get_by_role("button", name=re.compile(r"play", re.I))
            if btn.count() > 0:
                btn.first.click(timeout=2500)
                return
        except Exception:
            pass

        for selector in PLAY_SELECTORS:
            try:
                locator = frame.locator(selector)
                if locator.count() == 0:
                    continue
                locator.first.click(timeout=2500)
                return
            except Exception:
                continue

        try:
            video = frame.locator("video")
            if video.count() > 0:
                video.first.click(timeout=2500)
                return
        except Exception:
            continue


def start_video_playback(page: Page, capture_wait: int) -> None:
    """Click play repeatedly after the page loads, then wait for streams."""
    try:
        page.wait_for_load_state("load", timeout=30000)
    except Exception:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

    print(f"→ page loaded, waiting {POST_LOAD_DELAY}s before play clicks...")
    time.sleep(POST_LOAD_DELAY)

    for attempt in range(1, PLAY_CLICK_ATTEMPTS + 1):
        print(f"→ click play attempt {attempt}/{PLAY_CLICK_ATTEMPTS}")
        _click_play(page)
        if attempt < PLAY_CLICK_ATTEMPTS:
            time.sleep(PLAY_CLICK_INTERVAL)

    wait_for_streams(capture_wait)


def wait_for_streams(seconds: int) -> None:
    print(f"\nWaiting {seconds}s for video streams to load...")
    time.sleep(seconds)


def prompt_skip_file(cooldown: int = FILE_SKIP_COOLDOWN, title: Optional[str] = None) -> bool:
    """Wait up to cooldown seconds; Enter skips this file entirely."""
    if title:
        print(f"\nNext: {title}")
    print(f"Press Enter to skip this file ({cooldown}s)...", flush=True)
    skipped = {"value": False}

    def _read_enter():
        try:
            sys.stdin.readline()
            skipped["value"] = True
        except EOFError:
            pass

    reader = threading.Thread(target=_read_enter, daemon=True)
    reader.start()
    reader.join(timeout=cooldown)

    if skipped["value"]:
        print("→ skipping this file")
        return True

    print("→ opening and capturing")
    return False


def build_file_paths(safe_title: str, temp_dir: str, output_dir: str):
    base = safe_title or "output"
    video_tmp = os.path.join(temp_dir, f"{base}_video_tmp.mp4")
    audio_tmp = os.path.join(temp_dir, f"{base}_audio_tmp.m4a")
    output_mp4 = os.path.join(output_dir, f"{base}.mp4")
    output_m4a = os.path.join(output_dir, f"{base}.m4a")
    return video_tmp, audio_tmp, output_mp4, output_m4a


def download_captured_streams(
    captured_urls: Set[str],
    args,
    cookie_file: str,
    browser_user_agent: str,
    safe_title: str,
) -> bool:
    if not captured_urls:
        print("No urls captured.")
        return False

    parsed_streams = [parse_url_for_selection(u) for u in captured_urls]
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

    clean_video_url = strip_range_param(best_video["original_url"]) if best_video else None
    clean_audio_url = strip_range_param(best_audio["original_url"]) if best_audio else None

    dl_video = False
    dl_audio = False
    merge_needed = False

    if args.interactive:
        print("\n--- INTERACTIVE MODE ---")
        if best_video:
            print(f"🎥 Video found: {best_video['itag']} ({best_video['mime']})")
        if best_audio:
            print(f"🔊 Audio found: {best_audio['itag']} ({best_audio['mime']})")

        choice = input("\nDownload: (v)video, (a)audio, (b)both/merge [default: b]: ").lower() or "b"
        if choice == "v" and best_video:
            dl_video = True
        elif choice == "a" and best_audio:
            dl_audio = True
        elif choice == "b":
            dl_video = bool(best_video)
            dl_audio = bool(best_audio)
            merge_needed = dl_video and dl_audio
    else:
        dl_video = bool(best_video)
        dl_audio = bool(best_audio)
        merge_needed = dl_video and dl_audio
        print(f"\n--- AUTO-DOWNLOAD MODE ({'Both' if merge_needed else 'Single Stream'}) ---")

    if not dl_video and not dl_audio:
        print("Nothing selected to download.")
        return False

    video_tmp, audio_tmp, output_mp4, output_m4a = build_file_paths(
        safe_title, args.temp_dir, args.output_dir
    )
    results = [True, True]

    def download_worker(idx, label, url, output_file, user_agent, cookie_path, bar_pos):
        results[idx] = downloader.fast_download(
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


def process_single_video(
    page,
    context,
    url: str,
    args,
    cookie_file: str,
    index=None,
    total=None,
    title: Optional[str] = None,
):
    """Returns True on success, False on failure, None if skipped."""
    label = ""
    if index is not None and total is not None:
        label = f"[{index}/{total}] "

    display = (title or "").strip() or url

    print(f"\n{'=' * 60}")
    print(f"{label}{display}")
    print(f"{'=' * 60}")

    if prompt_skip_file(args.skip_cooldown, title=display):
        return None

    captured_urls = set()
    attach_capture_handler(page, captured_urls)

    print(f"{label}Opening: {display}")
    page.goto(url)

    start_video_playback(page, args.wait)

    browser_user_agent = page.evaluate("navigator.userAgent")
    save_cookies(context, cookie_file)
    safe_title = get_safe_title(page)

    return download_captured_streams(
        captured_urls, args, cookie_file, browser_user_agent, safe_title
    )


def run_classroom_mode(page, context, classroom_url: str, args, cookie_file: str) -> None:
    print(f"Launching Browser to Classroom: {classroom_url}")
    page.goto(classroom_url)

    print("\nClassroom page is open — sign in and scroll if needed.")
    wait_for_streams(args.wait)

    drive_items = extract_drive_items(page)
    if not drive_items:
        print("\nNo Google Drive links found on this page.")
        print("Make sure attachments are visible, then try again.")
        return

    print(f"\nFound {len(drive_items)} Drive link(s):")
    for i, item in enumerate(drive_items, start=1):
        name = display_name_for_item(item)
        print(f"  {i}. {name}")
        if item["title"]:
            print(f"      {item['url']}")

    if args.interactive:
        proceed = input("\nProcess all listed videos? (Y/n): ").strip().lower()
        if proceed == "n":
            print("Cancelled.")
            return

    succeeded = 0
    skipped = 0
    for i, item in enumerate(drive_items, start=1):
        result = process_single_video(
            page,
            context,
            item["url"],
            args,
            cookie_file,
            index=i,
            total=len(drive_items),
            title=item["title"],
        )
        if result is None:
            skipped += 1
        elif result:
            succeeded += 1

    print(f"\nDone. Downloaded {succeeded}, skipped {skipped}, total {len(drive_items)}.")


def run_single_mode(page, context, url: str, args, cookie_file: str) -> None:
    if prompt_skip_file(args.skip_cooldown, title=url):
        return

    captured_urls = set()
    attach_capture_handler(page, captured_urls)

    print(f"Launching Browser to: {url}")
    page.goto(url)

    start_video_playback(page, args.wait)

    browser_user_agent = page.evaluate("navigator.userAgent")
    save_cookies(context, cookie_file)
    safe_title = get_safe_title(page)

    download_captured_streams(
        captured_urls, args, cookie_file, browser_user_agent, safe_title
    )


def main():
    parser = argparse.ArgumentParser(description="Google Drive Media Capture")
    parser.add_argument(
        "url",
        help="Google Drive view URL or Google Classroom post URL",
    )
    parser.add_argument(
        "-i", "--interactive", action="store_true", help="Ask for download options"
    )
    parser.add_argument(
        "-w",
        "--wait",
        type=int,
        default=DEFAULT_CAPTURE_WAIT,
        help=f"Seconds to wait for streams before downloading (default: {DEFAULT_CAPTURE_WAIT})",
    )
    parser.add_argument(
        "--skip-cooldown",
        type=int,
        default=FILE_SKIP_COOLDOWN,
        help=f"Seconds to press Enter and skip a file before opening it (default: {FILE_SKIP_COOLDOWN})",
    )
    parser.add_argument(
        "--temp-dir",
        default=DEFAULT_TEMP_DIR,
        help=f"Directory for temporary download files (default: {DEFAULT_TEMP_DIR})",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for finished videos (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()

    args.temp_dir = os.path.abspath(args.temp_dir)
    args.output_dir = os.path.abspath(args.output_dir)
    os.makedirs(args.temp_dir, exist_ok=True)
    os.makedirs(args.output_dir, exist_ok=True)

    cookie_file = os.path.join(args.temp_dir, "cookies.txt")
    user_data_dir = os.path.expanduser("~/.config/google-chrome")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()

        if is_classroom_url(args.url):
            run_classroom_mode(page, context, args.url, args, cookie_file)
        else:
            run_single_mode(page, context, args.url, args, cookie_file)

        context.close()

    print(f"\nTemp files: {args.temp_dir}")
    print(f"Downloads:  {args.output_dir}")


if __name__ == "__main__":
    main()
