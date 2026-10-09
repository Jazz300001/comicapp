"""Read-only archive access for .cbz (zip) and .cbr (rar) - and for the files that
are named one thing and are really another.

Rules that are not negotiable:
  * archives are opened READ ONLY - never extracted next to, never written next
    to, never renamed, no sidecar files (a tool that needs a file on disk gets a
    system temp folder, which is deleted again);
  * the container is decided by MAGIC BYTES, never by the extension: a .cbr that
    is really a zip opens with the standard library and needs no external tool at
    all, which is common in the wild;
  * pages are listed in natural filename order (page2 before page10);
  * when nothing on the PC can read a genuine RAR, the file is still indexed but
    recorded as an error row with one short, actionable sentence.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
import zipfile
from typing import Iterable

from . import tools as tools_mod

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

#: container names stored on every row (`archive_container`)
CONTAINER_ZIP = "zip"
CONTAINER_RAR4 = "rar4"
CONTAINER_RAR5 = "rar5"
CONTAINER_7Z = "7z"
CONTAINER_UNKNOWN = "unknown"

RAR_CONTAINERS = (CONTAINER_RAR4, CONTAINER_RAR5)

#: (magic bytes, container) - longest signatures first
MAGIC_SIGNATURES = (
    (b"Rar!\x1a\x07\x01\x00", CONTAINER_RAR5),
    (b"Rar!\x1a\x07\x00", CONTAINER_RAR4),
    (b"7z\xbc\xaf\x27\x1c", CONTAINER_7Z),
    (b"PK\x03\x04", CONTAINER_ZIP),
    (b"PK\x05\x06", CONTAINER_ZIP),   # empty archive
    (b"PK\x07\x08", CONTAINER_ZIP),   # spanned archive
)

#: what each extension is supposed to hold
EXPECTED_CONTAINER = {".cbz": (CONTAINER_ZIP,), ".cbr": RAR_CONTAINERS}

#: error kinds, so the UI can group 165 identical reasons into one line
ERROR_RAR_TOOL_MISSING = "rar_tool_missing"
ERROR_7Z_TOOL_MISSING = "sevenzip_tool_missing"
ERROR_TOOL_FAILED = "archive_tool_failed"
ERROR_CONTAINER_UNKNOWN = "container_unknown"
ERROR_CORRUPT_ZIP = "corrupt_zip"
ERROR_READ_FAILED = "archive_unreadable"

#: kept for backwards compatibility with the first version of the scanner
UNRAR_MISSING_MESSAGE = tools_mod.RAR_TOOL_MISSING_MESSAGE
#: historical name of the same thing
RAR_TOOLS = tools_mod.TOOL_ORDER

IMAGE_MAGICS = (
    b"\xff\xd8\xff",              # jpeg
    b"\x89PNG\r\n\x1a\n",         # png
    b"GIF87a", b"GIF89a",         # gif
    b"BM",                        # bmp
    b"II*\x00", b"MM\x00*",       # tiff
    b"RIFF",                      # webp (RIFF....WEBP)
)


class ArchiveError(Exception):
    """Archive could not be read (corrupt, unsupported, or no tool for it).

    ``kind`` is the machine-readable reason stored in the database so the UI's
    problems panel can group identical failures instead of listing them one by one.
    """

    def __init__(self, message: str, kind: str = ERROR_READ_FAILED,
                 container: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.container = container


def detect_container(path: str) -> str:
    """Container of a file by magic bytes - never by its extension."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError:
        return CONTAINER_UNKNOWN
    for signature, container in MAGIC_SIGNATURES:
        if head.startswith(signature):
            return container
    return CONTAINER_UNKNOWN


def extension_container(path: str) -> str | None:
    """What the file *claims* to be, from its name (``None`` when it is neither)."""
    ext = os.path.splitext(str(path))[1].casefold()
    return {".cbz": CONTAINER_ZIP, ".cbr": CONTAINER_RAR4}.get(ext)


def container_mismatch(path: str, container: str | None) -> int:
    """1 when the extension disagrees with the real contents, else 0.

    An unrecognised container is 'corrupt', not 'misnamed', so it is not a
    mismatch.  A .cbr that is really a zip *is* a mismatch, and a .cbz that is
    really a rar is one too.
    """
    if not container or container == CONTAINER_UNKNOWN:
        return 0
    ext = os.path.splitext(str(path))[1].casefold()
    expected = EXPECTED_CONTAINER.get(ext)
    if expected is None:
        return 0
    return 0 if container in expected else 1


def rar_tool_available() -> bool:
    """True when some rar-capable executable works on this PC."""
    return tools_mod.resolve() is not None


def archive_type(path: str) -> str | None:
    """The extension label ('cbz'/'cbr') - a label, not a statement about content."""
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


def looks_like_image(data: bytes) -> bool:
    """True when the bytes start with something an image can start with."""
    return any(data.startswith(magic) for magic in IMAGE_MAGICS)


def plausible_image_bytes(data: bytes) -> bool:
    """Is this good enough to hand to the browser as a page?

    A streamed page that came back holding a tool's chit-chat instead of pixels is
    printable ASCII and short; a real image either matches a known magic or is
    binary and substantial.  Used to decide whether a stdout read needs retrying
    through a temp folder.
    """
    if not data:
        return False
    if looks_like_image(data):
        return True
    head = data[:512]
    printable = sum(1 for byte in head if 32 <= byte < 127 or byte in (9, 10, 13))
    return not (len(data) < 512 or printable == len(head))


class Archive:
    """Context manager over one read-only archive."""

    def __init__(self, path: str):
        self.path = path
        self.extension_type = archive_type(path)     # 'cbz' / 'cbr' from the name
        self.container = detect_container(path)      # what the bytes really are
        self.kind = self.container                   # backwards-compatible alias
        self.mismatch = container_mismatch(path, self.container)
        self.tool = None
        self._handle = None
        self._names: list[str] = []
        self._temp_dir: str | None = None

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "Archive":
        try:
            if self.container == CONTAINER_ZIP:
                self._open_zip()
            elif self.container in RAR_CONTAINERS or self.container == CONTAINER_7Z:
                self._open_with_tool()
            else:
                raise ArchiveError(
                    f"this is not a comic archive: no zip, rar or 7z signature in {self.path}",
                    kind=ERROR_CONTAINER_UNKNOWN, container=self.container)
        except ArchiveError:
            self.close()
            raise
        except zipfile.BadZipFile as exc:
            self.close()
            raise ArchiveError(f"corrupt zip (not a readable CBZ): {exc}",
                               kind=ERROR_CORRUPT_ZIP, container=self.container) from exc
        except Exception as exc:
            self.close()
            raise ArchiveError(f"could not open archive: {exc}",
                               container=self.container) from exc
        return self

    def _open_zip(self) -> None:
        """A zip is a zip: the stdlib reads it whatever the file is called."""
        try:
            self._handle = zipfile.ZipFile(self.path, "r")
        except zipfile.BadZipFile as exc:
            raise ArchiveError(f"corrupt zip (not a readable CBZ): {exc}",
                               kind=ERROR_CORRUPT_ZIP, container=self.container) from exc
        try:
            self._names = list(self._handle.namelist())
        except Exception as exc:
            raise ArchiveError(f"could not list archive contents: {exc}",
                               kind=ERROR_READ_FAILED, container=self.container) from exc

    def _open_with_tool(self) -> None:
        missing = (tools_mod.RAR_TOOL_MISSING_MESSAGE
                   if self.container in RAR_CONTAINERS
                   else tools_mod.SEVENZ_MISSING_MESSAGE)
        missing_kind = (ERROR_RAR_TOOL_MISSING if self.container in RAR_CONTAINERS
                        else ERROR_7Z_TOOL_MISSING)
        first = tools_mod.resolve()
        if first is None:
            raise ArchiveError(missing, kind=missing_kind, container=self.container)
        last_error: Exception | None = None
        for tool in tools_mod.remaining_after(first):
            try:
                if tool.name == "unar":
                    # unar can only list by unpacking; keep the folder for this
                    # open so reads come from it instead of unpacking again
                    self._ensure_temp_dir()
                    tool.extract_all(self.path, self._temp_dir)
                    names = sorted(self._relative_files())
                else:
                    names = tool.list_names(self.path)
            except tools_mod.ToolError as exc:
                tools_mod.report_failure(tool)
                last_error = exc
                continue
            tools_mod.report_success(tool)
            self.tool = tool
            self._names = names
            return
        label = "RAR" if self.container in RAR_CONTAINERS else "7z"
        if last_error is None:
            raise ArchiveError(missing, kind=missing_kind, container=self.container)
        raise ArchiveError(
            f"{label} could not be opened with the tools on this PC "
            f"({last_error})", kind=ERROR_TOOL_FAILED, container=self.container)

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

    # -- temp folder (never next to the comics) ----------------------------
    def _ensure_temp_dir(self) -> str:
        if self._temp_dir is None:
            self._temp_dir = tempfile.mkdtemp(prefix="longbox-rar-")
        return self._temp_dir

    def _relative_files(self) -> list[str]:
        found = []
        for dirpath, _dirnames, filenames in os.walk(self._temp_dir or ""):
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                found.append(os.path.relpath(full, self._temp_dir).replace(os.sep, "/"))
        return found

    def _temp_member_path(self, member: str) -> str | None:
        if self._temp_dir is None:
            return None
        wanted = member.replace("\\", "/").casefold()
        for name in self._relative_files():
            if name.casefold() == wanted:
                return os.path.join(self._temp_dir, *name.split("/"))
        return None

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
        if self._handle is not None:  # zip
            try:
                with self._handle.open(member) as handle:
                    return handle.read()
            except Exception as exc:
                raise ArchiveError(f"could not read '{member}': {exc}",
                                   container=self.container) from exc
        return self._read_with_tool(member)

    def _read_with_tool(self, member: str) -> bytes:
        local = self._temp_member_path(member)
        if local:  # already unpacked for this open
            try:
                with open(local, "rb") as handle:
                    return handle.read()
            except OSError as exc:
                raise ArchiveError(f"could not read '{member}': {exc}",
                                   container=self.container) from exc
        if self.tool is None:  # pragma: no cover - open() always resolves one
            raise ArchiveError(tools_mod.RAR_TOOL_MISSING_MESSAGE,
                               kind=ERROR_RAR_TOOL_MISSING, container=self.container)
        streamed = None
        try:
            streamed = self.tool.read_member(self.path, member)
        except tools_mod.ToolError as exc:
            streamed = None
            stream_error = exc
        if streamed is not None and (not is_image_member(member)
                                     or plausible_image_bytes(streamed)):
            return streamed
        # stdout gave us chatter (or nothing) instead of the page: unpack just this
        # member into a temp folder and read it from there
        temp = tempfile.mkdtemp(prefix="longbox-rar-")
        try:
            try:
                self.tool.extract_member(self.path, member, temp)
            except tools_mod.ToolError as exc:
                raise ArchiveError(
                    f"could not read '{member}' from the archive ({exc})",
                    kind=ERROR_TOOL_FAILED, container=self.container) from exc
            for dirpath, _dirnames, filenames in os.walk(temp):
                for filename in filenames:
                    if filename.casefold() == os.path.basename(member).casefold():
                        with open(os.path.join(dirpath, filename), "rb") as handle:
                            return handle.read()
            raise ArchiveError(f"'{member}' is not in the archive",
                               container=self.container)
        finally:
            shutil.rmtree(temp, ignore_errors=True)


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
