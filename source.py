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
import ytdlp_downloader

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


def auto_scroll_to_bottom(page, scroll_pause: float = 2.5, max_unchanged: int = 3) -> None:
    """Scroll an infinite-scroll page to the bottom, waiting for new content after each step.

    Keeps scrolling until the page height stops growing for *max_unchanged*
    consecutive checks, which signals that all content has been loaded.
    """
    print("\n→ Auto-scrolling to load all classroom content...")
    prev_height: int = -1
    unchanged = 0
    scroll_n = 0

    while unchanged < max_unchanged:
        page.evaluate(
            "window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})"
        )
        time.sleep(scroll_pause)

        new_height: int = page.evaluate("document.body.scrollHeight")
        scroll_n += 1

        if new_height == prev_height:
            unchanged += 1
            print(f"   no new content ({unchanged}/{max_unchanged})...")
        else:
            unchanged = 0
            prev_height = new_height
            print(f"   scroll {scroll_n}: more content loaded (page height: {new_height}px)")

    print(f"✓ Reached bottom after {scroll_n} scroll(s).\n")


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


def run_classroom_ytdlp_mode(
    page, context, classroom_url: str, args, cookie_file: str
) -> None:
    """Default classroom mode: extract links → save cookies → yt-dlp downloads.

    Playwright is only used for link extraction and cookie export.  All actual
    downloading is handled by yt-dlp outside the browser, one video at a time.
    """
    print(f"Launching Browser to Classroom: {classroom_url}")
    page.goto(classroom_url)

    print("\nClassroom page is open.")
    print("Sign in to your Google account if prompted, then navigate to the")
    print("classroom page with the video attachments visible.")
    try:
        input("\nPress Enter when you are on the classroom page and signed in...")
    except EOFError:
        pass

    # Automatically scroll through the infinite-scroll page to reveal all links
    auto_scroll_to_bottom(page)

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
        proceed = input("\nDownload all listed videos with yt-dlp? (Y/n): ").strip().lower()
        if proceed == "n":
            print("Cancelled.")
            return

    # Export cookies while the browser session is still live
    cookie.save_cookies(context, cookie_file)

    # Write the URL list that yt-dlp will work through
    url_list_file = ytdlp_downloader.save_url_list(video_items, args.temp_dir)

    # Browser is no longer needed — close it before the long download starts
    print("\n→ Closing browser and handing off to yt-dlp...")
    context.close()

    ytdlp_downloader.download_with_ytdlp(
        url_list_file=url_list_file,
        cookie_file=cookie_file,
        output_dir=args.output_dir,
        use_aria2c=getattr(args, "aria2c", False),
        fragments=getattr(args, "fragments", 0),
    )


def run_classroom_mode(
    page, context, classroom_url: str, args, cookie_file: Optional[str]
) -> None:
    """Custom stream-capture classroom mode (opt-in via --custom flag).

    Opens each Drive video page, captures the raw video/audio stream URLs via
    Playwright network interception, and downloads them with the built-in
    parallel downloader.
    """
    print(f"Launching Browser to Classroom: {classroom_url}")
    page.goto(classroom_url)

    print("\nClassroom page is open.")
    print("Sign in to your Google account if prompted, then navigate to the")
    print("classroom page with the video attachments visible.")
    try:
        input("\nPress Enter when you are on the classroom page and signed in...")
    except EOFError:
        pass

    # Automatically scroll through the infinite-scroll page to reveal all links
    auto_scroll_to_bottom(page)

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

def run_setup_mode(temp_dir: str) -> None:
    """First-launch setup: open Google Drive so the user can sign in.

    Playwright is launched with --disable-blink-features=AutomationControlled
    which prevents Google from showing the 'This app may not be secure' banner
    that appears when it detects browser automation.

    The user signs in normally, accepts any Google security prompts, and presses
    Enter when done.  Their session cookies are then saved for future runs.
    """
    os.makedirs(temp_dir, exist_ok=True)
    cookie_file = cookie.cookie_path(temp_dir)
    user_data_dir = os.path.expanduser("~/.config/google-chrome")

    print("\n" + "=" * 60)
    print("  SETUP MODE")
    print("=" * 60)
    print("Opening Google Drive in the browser.")
    print("\nSteps:")
    print("  1. Sign in to your Google account if prompted.")
    print("  2. If you see 'This app may not be secure', click Continue.")
    print("  3. Navigate to a Classroom or Drive page to confirm access.")
    print("  4. Press Enter here when you are done.")
    print("=" * 60 + "\n")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            viewport={"width": 1400, "height": 900},
            # Suppress Playwright's automation fingerprint so Google does not
            # show the 'This app may not be secure' warning.
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()

        # Remove the webdriver property that Google checks
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        print("→ Navigating to Google Drive...")
        page.goto("https://drive.google.com")

        # Block until the user confirms they are done
        try:
            input("\nPress Enter when signed in and all security prompts are cleared...")
        except EOFError:
            pass

        cookie.save_cookies(context, cookie_file)
        context.close()

    print("\n✅ Setup complete!")
    print(f"   Cookies saved to: {cookie_file}")
    print("\nYou can now run the downloader normally, e.g.:")
    print("   python source.py <classroom-url>")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Google Drive / Classroom Video Downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # First-time setup (sign in + fix 'not secure' warning)\n"
            "  python source.py --setup\n"
            "\n"
            "  # Classroom URL — yt-dlp + aria2c (fastest)\n"
            "  python source.py -a https://classroom.google.com/c/.../p/...\n"
            "\n"
            "  # Classroom URL — yt-dlp with 16 concurrent fragments\n"
            "  python source.py -n 16 https://classroom.google.com/c/.../p/...\n"
            "\n"
            "  # Classroom URL — custom stream-capture mode\n"
            "  python source.py --custom https://classroom.google.com/c/.../p/...\n"
            "\n"
            "  # Single Drive video\n"
            "  python source.py https://drive.google.com/file/d/.../view\n"
        ),
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="Google Drive view URL or Google Classroom post URL (not required with --setup)",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help=(
            "First-launch setup: open Google Drive in the browser so you can "
            "sign in and clear the 'This app may not be secure' warning."
        ),
    )
    parser.add_argument(
        "--custom",
        action="store_true",
        help=(
            "Use the built-in stream-capture downloader instead of yt-dlp. "
            "Only applies to Classroom URLs."
        ),
    )
    parser.add_argument(
        "-i", "--interactive", action="store_true", help="Ask for download options"
    )
    parser.add_argument(
        "-w",
        "--wait",
        type=int,
        default=DEFAULT_CAPTURE_WAIT,
        help=f"Seconds to wait for the page to load / streams to appear (default: {DEFAULT_CAPTURE_WAIT})",
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
        help=f"Directory for temporary files (default: {DEFAULT_TEMP_DIR})",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for finished videos (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "-a",
        "--aria2c",
        action="store_true",
        help="Use aria2c as the yt-dlp external downloader (-x 16 -k 1M). Fastest option.",
    )
    parser.add_argument(
        "-n",
        "--fragments",
        type=int,
        default=0,
        metavar="N",
        help="Use yt-dlp's built-in concurrent-fragment downloader with N connections (e.g. -n 16).",
    )
    parser.add_argument(
        "-c",
        "--cookies",
        action="store_true",
        help="Export browser cookies and use them for downloads (always enabled for yt-dlp mode)",
    )
    args = parser.parse_args()

    # --setup does not need a URL
    if args.setup:
        temp_dir = os.path.abspath(
            getattr(args, "temp_dir", DEFAULT_TEMP_DIR)
        )
        run_setup_mode(temp_dir)
        return

    if not args.url:
        parser.error("the following arguments are required: url (or use --setup)")
    args.temp_dir = os.path.abspath(args.temp_dir)
    args.output_dir = os.path.abspath(args.output_dir)
    os.makedirs(args.temp_dir, exist_ok=True)
    os.makedirs(args.output_dir, exist_ok=True)

    # yt-dlp mode always needs cookies for private Drive videos
    use_ytdlp = is_classroom_url(args.url) and not args.custom
    needs_cookies = args.cookies or use_ytdlp
    cookie_file = cookie.cookie_path(args.temp_dir) if needs_cookies else None

    user_data_dir = os.path.expanduser("~/.config/google-chrome")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            viewport={"width": 1400, "height": 900},
            # Prevent Google from flagging the session as automated
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        # Remove the automation marker from the JS environment
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        if is_classroom_url(args.url):
            if args.custom:
                # Legacy: open each video in the browser and capture raw streams
                run_classroom_mode(page, context, args.url, args, cookie_file)
                context.close()
            else:
                # Default: extract links + cookies, then let yt-dlp do the work.
                # run_classroom_ytdlp_mode closes the context itself before downloading.
                run_classroom_ytdlp_mode(page, context, args.url, args, cookie_file)
        else:
            run_single_mode(page, context, args.url, args, cookie_file)
            context.close()

    print(f"\nTemp files: {args.temp_dir}")
    print(f"Downloads:  {args.output_dir}")


if __name__ == "__main__":
    main()
