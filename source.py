import argparse
import os

from playwright.sync_api import sync_playwright

from lib import config, cookie

DEFAULT_CAPTURE_WAIT = config.get("CAPTURE_WAIT")
FILE_SKIP_COOLDOWN = config.get("SKIP_COOLDOWN")
DEFAULT_TEMP_DIR = config.get("TEMP_DIR")
DEFAULT_OUTPUT_DIR = config.get("OUTPUT_DIR")


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
        default=config.get("CUSTOM_MODE"),
        help=(
            "Use the built-in stream-capture downloader instead of yt-dlp. "
            "Only applies to Classroom URLs."
        ),
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        default=config.get("INTERACTIVE"),
        help="Ask for download options",
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
        default=config.get("USE_ARIA2C"),
        help="Use aria2c as the yt-dlp external downloader (-x 16 -k 1M). Fastest option.",
    )
    parser.add_argument(
        "-n",
        "--fragments",
        type=int,
        default=config.get("YTDLP_FRAGMENTS"),
        metavar="N",
        help="Use yt-dlp's built-in concurrent-fragment downloader with N connections (e.g. -n 16).",
    )
    parser.add_argument(
        "-c",
        "--cookies",
        action="store_true",
        default=config.get("USE_COOKIES"),
        help="Export browser cookies and use them for downloads (always enabled for yt-dlp mode)",
    )
    args = parser.parse_args()

    from lib.runner import (
        run_classroom_mode,
        run_classroom_ytdlp_mode,
        run_setup_mode,
        run_single_mode,
    )
    from lib.sanitize import is_classroom_url

    if args.setup:
        temp_dir = os.path.abspath(getattr(args, "temp_dir", DEFAULT_TEMP_DIR))
        run_setup_mode(temp_dir)
        return

    if not args.url:
        parser.error("the following arguments are required: url (or use --setup)")
    args.temp_dir = os.path.abspath(args.temp_dir)
    args.output_dir = os.path.abspath(args.output_dir)
    os.makedirs(args.temp_dir, exist_ok=True)
    os.makedirs(args.output_dir, exist_ok=True)

    use_ytdlp = is_classroom_url(args.url) and not args.custom
    needs_cookies = args.cookies or use_ytdlp
    cookie_file = cookie.cookie_path(args.temp_dir) if needs_cookies else None

    user_data_dir = os.path.expanduser("~/.config/google-chrome")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            viewport={"width": 1400, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        if is_classroom_url(args.url):
            if args.custom:
                run_classroom_mode(page, context, args.url, args, cookie_file)
                context.close()
            else:
                run_classroom_ytdlp_mode(page, context, args.url, args, cookie_file)
        else:
            run_single_mode(page, context, args.url, args, cookie_file)
            context.close()

    print(f"\nTemp files: {args.temp_dir}")
    print(f"Downloads:  {args.output_dir}")


if __name__ == "__main__":
    main()
