"""Video vs non-video filtering for Drive links and captured streams."""

from typing import List, Optional, Tuple, TypedDict

from playwright.sync_api import Page

NON_VIDEO_EXTENSIONS = (
    ".pdf",
    ".ppt",
    ".pptx",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".txt",
    ".rtf",
    ".odt",
    ".ods",
    ".odp",
    ".csv",
    ".zip",
    ".rar",
    ".7z",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".bmp",
    ".html",
    ".htm",
    ".json",
    ".xml",
    ".epub",
    ".pages",
    ".numbers",
    ".key",
)

VIDEO_EXTENSIONS = (
    ".mp4",
    ".mov",
    ".webm",
    ".mkv",
    ".avi",
    ".m4v",
    ".wmv",
    ".flv",
    ".mpeg",
    ".mpg",
    ".3gp",
    ".m2ts",
)


class DriveItem(TypedDict):
    url: str
    title: str


def _text_has_extension(text: str, extensions: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(ext in lower for ext in extensions)


def title_suggests_non_video(title: str) -> bool:
    """True when the title/label clearly refers to a non-video file."""
    return bool(title) and _text_has_extension(title, NON_VIDEO_EXTENSIONS)


def title_suggests_video(title: str) -> bool:
    return bool(title) and _text_has_extension(title, VIDEO_EXTENSIONS)


def is_capturable_media_response(url: str, content_type: str) -> bool:
    """True when a network response should be treated as a video/audio stream."""
    u = url.lower()
    ct = content_type.lower()
    return "videoplayback" in u or ct.startswith(("video/", "audio/"))


def is_drive_video_page(page: Page) -> bool:
    """Return True if the open Drive page appears to be a video, not a document."""
    title = page.title() or ""
    if title_suggests_non_video(title):
        return False

    for frame in page.frames:
        try:
            if frame.locator("video").count() > 0:
                return True
        except Exception:
            continue

    for frame in page.frames:
        for selector in (
            '[aria-label="Play video"]',
            '[aria-label*="Play video" i]',
        ):
            try:
                if frame.locator(selector).count() > 0:
                    return True
            except Exception:
                continue

    if title_suggests_video(title):
        return True

    return False


def filter_video_items(
    items: List[DriveItem],
) -> Tuple[List[DriveItem], List[DriveItem]]:
    """Split items into videos to process and non-videos skipped by title."""
    videos: List[DriveItem] = []
    skipped: List[DriveItem] = []
    for item in items:
        if title_suggests_non_video(item["title"]):
            skipped.append(item)
        else:
            videos.append(item)
    return videos, skipped


def should_skip_before_open(title: str) -> bool:
    """True if the item should be skipped without opening the Drive page."""
    return title_suggests_non_video(title)
