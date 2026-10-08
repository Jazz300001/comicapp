"""Read-only archive access for .cbz (zip) and .cbr (rar).

Rules that are not negotiable:
  * archives are opened READ ONLY - never extracted, never written next to,
    never renamed, no sidecar files;
  * pages are listed in natural filename order (page2 before page10);
  * if no unrar tool exists, a .cbr is still indexed but recorded as an error
    ("unrar not installed - CBR not readable") instead of pretending it worked.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import zipfile
from typing import Iterable

ARCHIVE_EXTENSIONS = (".cbz", ".cbr")

IMAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".avif",
)

CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg",
    ".jfif": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".avif": "image/avif",
}

#: executable names rarfile can drive
RAR_TOOLS = ("unrar", "unar", "bsdtar", "7z", "7zz", "7za")

UNRAR_MISSING_MESSAGE = "unrar not installed - CBR not readable"


class ArchiveError(Exception):
    """Archive could not be read (corrupt, unsupported, or tool missing)."""


def rar_tool_available() -> bool:
    """True when some rar-capable executable is on PATH."""
    for tool in RAR_TOOLS:
        if shutil.which(tool):
            return True
    try:  # rarfile also honours an explicitly configured tool path
        import rarfile

        for candidate in (getattr(rarfile, "UNRAR_TOOL", None),
                          getattr(rarfile, "UNAR_TOOL", None),
                          getattr(rarfile, "BSDTAR_TOOL", None)):
            if candidate and shutil.which(candidate):
                return True
    except Exception:  # pragma: no cover - rarfile missing is not fatal
        return False
    return False


def archive_type(path: str) -> str | None:
    ext = os.path.splitext(str(path))[1].casefold()
    if ext == ".cbz":
        return "cbz"
    if ext == ".cbr":
        return "cbr"
    return None


def natural_key(name: str):
    """Sort key that puts page2 before page10 and ignores case."""
    parts = re.split(r"(\d+)", str(name).casefold())
    return tuple((1, int(part)) if part.isdigit() else (0, part) for part in parts)


def is_image_member(name: str) -> bool:
    path = name.replace("\\", "/")
    if path.endswith("/"):
        return False
    parts = path.split("/")
    if any(part.startswith(".") for part in parts):  # .DS_Store, .hidden dirs
        return False
    if any(part.casefold() == "__macosx" for part in parts):
        return False
    return os.path.splitext(path)[1].casefold() in IMAGE_EXTENSIONS


def content_type_for(name: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(name)[1].casefold(), "application/octet-stream")


def sort_pages(names: Iterable[str]) -> list[str]:
    return sorted(names, key=natural_key)


def find_comicinfo(names: Iterable[str]) -> str | None:
    """Case-insensitive ComicInfo.xml lookup, shallowest first."""
    candidates = [
        name for name in names
        if os.path.basename(name.replace("\\", "/")).casefold() == "comicinfo.xml"
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda n: (n.count("/"), len(n), n.casefold()))[0]


class Archive:
    """Context manager over one read-only archive."""

    def __init__(self, path: str):
        self.path = path
        self.kind = archive_type(path)
        self._handle = None
        self._names: list[str] = []

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "Archive":
        try:
            if self.kind == "cbz":
                self._handle = zipfile.ZipFile(self.path, "r")
            elif self.kind == "cbr":
                if not rar_tool_available():
                    raise ArchiveError(UNRAR_MISSING_MESSAGE)
                import rarfile

                try:
                    self._handle = rarfile.RarFile(self.path, "r")
                except Exception as exc:  # rarfile.Error and friends
                    if isinstance(exc, rarfile.NeedFirstVolume):
                        raise ArchiveError(f"multi-volume rar, first part missing: {exc}") from exc
                    raise ArchiveError(f"CBR could not be opened: {exc}") from exc
            else:
                raise ArchiveError(f"unsupported archive type: {self.path}")
        except zipfile.BadZipFile as exc:
            raise ArchiveError(f"corrupt zip (not a readable CBZ): {exc}") from exc
        except ArchiveError:
            raise
        except Exception as exc:
            raise ArchiveError(f"could not open archive: {exc}") from exc
        try:
            self._names = [item for item in self._handle.namelist()]
        except Exception as exc:
            self.close()
            raise ArchiveError(f"could not list archive contents: {exc}") from exc
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None

    # -- content -----------------------------------------------------------
    @property
    def names(self) -> list[str]:
        return list(self._names)

    def pages(self) -> list[str]:
        """Image members in natural filename order."""
        return sort_pages(name for name in self._names if is_image_member(name))

    def comicinfo_member(self) -> str | None:
        return find_comicinfo(self._names)

    def read(self, member: str) -> bytes:
        try:
            with self._handle.open(member) as handle:
                return handle.read()
        except Exception as exc:
            raise ArchiveError(f"could not read '{member}': {exc}") from exc


def open_archive(path: str) -> Archive:
    return Archive(path)


def cover_position(pages: list[str], comicinfo_pages: list[dict] | None) -> int:
    """1-based position of the front cover inside ``pages``.

    ComicInfo's ``Image`` index is the archive image index (0 = front cover).
    When the ComicInfo page list disagrees with reality we fall back to page 1.
    """
    if comicinfo_pages:
        front = [
            page for page in comicinfo_pages
            if str(page.get("type") or "").casefold() == "frontcover"
        ]
        for page in front + list(comicinfo_pages):
            index = page.get("image")
            if isinstance(index, int) and 0 <= index < len(pages):
                return index + 1
    return 1


def make_thumbnail(data: bytes, max_side: int = 400, quality: int = 80) -> bytes:
    """Downscale image bytes to a JPEG thumbnail (kept in memory)."""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise ArchiveError("Pillow is not installed, cannot build thumbnails") from exc

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.mode not in ("RGB", "L"):
            background = Image.new("RGB", image.size, (255, 255, 255))
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                background.paste(rgba, mask=rgba.split()[-1])
            else:
                background.paste(image.convert("RGB"))
            image = background
        elif image.mode == "L":
            image = image.convert("RGB")
        image.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality)
        return buffer.getvalue()
