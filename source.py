import argparse
import os
import re
import sys
import threading
import time
from typing import List, Optional, Set

DEFAULT_CAPTURE_WAIT = 15
POST_LOAD_DELAY = 1
PLAY_CLICK_ATTEMPTS = 3
PLAY_CLICK_INTERVAL = 2
FILE_SKIP_COOLDOWN = 3
DEFAULT_TEMP_DIR = ".tmp"
DEFAULT_OUTPUT_DIR = "downloads"

from playwright.sync_api import Page, sync_playwright

import cookie
import downloader
import filter

DRIVE_FILE_RE = re.compile(
    r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)", re.IGNORECASE
)
DRIVE_OPEN_RE = re.compile(
    r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)", re.IGNORECASE
)


DriveItem = filter.DriveItem


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
        ct = response.headers.get("content-type", "")
        if not filter.is_capturable_media_response(response.url, ct):
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


def process_single_video(
    page,
    context,
    url: str,
    args,
    cookie_file: Optional[str],
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

    if filter.should_skip_before_open(display):
        print("→ skipping (not a video — title suggests document/image)")
        return None

    if prompt_skip_file(args.skip_cooldown, title=display):
        return None

    captured_urls = set()
    attach_capture_handler(page, captured_urls)

    print(f"{label}Opening: {display}")
    page.goto(url)

    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    time.sleep(POST_LOAD_DELAY)

    if not filter.is_drive_video_page(page):
        print("→ skipping (not a video — no video player on Drive page)")
        return None

    start_video_playback(page, args.wait)

    browser_user_agent = page.evaluate("navigator.userAgent")
    if cookie_file:
        cookie.save_cookies(context, cookie_file)
    safe_title = get_safe_title(page)

    return downloader.download_captured_streams(
        captured_urls,
        interactive=args.interactive,
        temp_dir=args.temp_dir,
        output_dir=args.output_dir,
        cookie_file=cookie_file,
        browser_user_agent=browser_user_agent,
        safe_title=safe_title,
    )


def run_classroom_mode(
    page, context, classroom_url: str, args, cookie_file: Optional[str]
) -> None:
    print(f"Launching Browser to Classroom: {classroom_url}")
    page.goto(classroom_url)

    print("\nClassroom page is open — sign in and scroll if needed.")
    wait_for_streams(args.wait)

    drive_items = extract_drive_items(page)
    if not drive_items:
        print("\nNo Google Drive links found on this page.")
        print("Make sure attachments are visible, then try again.")
        return

    video_items, title_skipped = filter.filter_video_items(drive_items)

    print(f"\nFound {len(drive_items)} Drive link(s), {len(video_items)} video(s):")
    for i, item in enumerate(video_items, start=1):
        name = display_name_for_item(item)
        print(f"  {i}. {name}")
        if item["title"]:
            print(f"      {item['url']}")

    if title_skipped:
        print(f"\nSkipped {len(title_skipped)} non-video attachment(s) by title:")
        for item in title_skipped:
            print(f"  - {display_name_for_item(item)}")

    if not video_items:
        print("\nNo video attachments to process.")
        return

    if args.interactive:
        proceed = input("\nProcess all listed videos? (Y/n): ").strip().lower()
        if proceed == "n":
            print("Cancelled.")
            return

    succeeded = 0
    skipped = len(title_skipped)
    for i, item in enumerate(video_items, start=1):
        result = process_single_video(
            page,
            context,
            item["url"],
            args,
            cookie_file,
            index=i,
            total=len(video_items),
            title=item["title"],
        )
        if result is None:
            skipped += 1
        elif result:
            succeeded += 1

    print(
        f"\nDone. Downloaded {succeeded}, skipped {skipped}, "
        f"videos {len(video_items)}, total links {len(drive_items)}."
    )


def run_single_mode(page, context, url: str, args, cookie_file: Optional[str]) -> None:
    if prompt_skip_file(args.skip_cooldown, title=url):
        return

    captured_urls = set()
    attach_capture_handler(page, captured_urls)

    print(f"Launching Browser to: {url}")
    page.goto(url)

    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    time.sleep(POST_LOAD_DELAY)

    if not filter.is_drive_video_page(page):
        print("→ not a video file (no video player on Drive page). Exiting.")
        return

    start_video_playback(page, args.wait)

    browser_user_agent = page.evaluate("navigator.userAgent")
    if cookie_file:
        cookie.save_cookies(context, cookie_file)
    safe_title = get_safe_title(page)

    downloader.download_captured_streams(
        captured_urls,
        interactive=args.interactive,
        temp_dir=args.temp_dir,
        output_dir=args.output_dir,
        cookie_file=cookie_file,
        browser_user_agent=browser_user_agent,
        safe_title=safe_title,
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
    parser.add_argument(
        "-c",
        "--cookies",
        action="store_true",
        help="Export browser cookies and use them for downloads",
    )
    args = parser.parse_args()

    args.temp_dir = os.path.abspath(args.temp_dir)
    args.output_dir = os.path.abspath(args.output_dir)
    os.makedirs(args.temp_dir, exist_ok=True)
    os.makedirs(args.output_dir, exist_ok=True)

    cookie_file = cookie.cookie_path(args.temp_dir) if args.cookies else None
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
