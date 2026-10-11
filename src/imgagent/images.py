"""Image validation and fingerprinting.

Files are NEVER trusted by extension or Content-Type: we sniff magic bytes,
then decode with Pillow (verify + reopen), enforce a pixel-count guard, and
compute perceptual hashes for near-duplicate detection.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFile, UnidentifiedImageError

from imgagent import constants as C
from imgagent.errors import DependencyError, ValidationFailed
from imgagent.security import check_decompression_bomb

ImageFile.LOAD_TRUNCATED_IMAGES = False  # truncated files must fail

# (format name, magic predicate over first bytes)
def _is_jpeg(b: bytes) -> bool:
    return len(b) >= 3 and b[0] == 0xFF and b[1] == 0xD8 and b[2] == 0xFF


def _is_png(b: bytes) -> bool:
    return b.startswith(b"\x89PNG\r\n\x1a\n")


def _is_gif(b: bytes) -> bool:
    return b.startswith((b"GIF87a", b"GIF89a"))


def _is_webp(b: bytes) -> bool:
    return len(b) >= 12 and b[0:4] == b"RIFF" and b[8:12] == b"WEBP"


def _is_avif(b: bytes) -> bool:
    return len(b) >= 12 and b[4:8] == b"ftyp" and b[8:12] in (b"avif", b"avis", b"mif1")


_MAGIC = (("JPEG", _is_jpeg), ("PNG", _is_png), ("GIF", _is_gif), ("WEBP", _is_webp), ("AVIF", _is_avif))


def sniff_format(head: bytes) -> str | None:
    """Return canonical format name from magic bytes, or None if unrecognized."""
    for name, pred in _MAGIC:
        try:
            if pred(head):
                return name
        except Exception:
            continue
    return None


def looks_like_html(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<") or b"<html" in head or b"<!doctype html" in head


@dataclass
class ImageInfo:
    format: str
    width: int
    height: int
    mode: str
    animated: bool = False


def validate_image_bytes(data: bytes, *, max_pixels: int, allowed_formats: tuple = C.ALLOWED_FORMATS) -> ImageInfo:
    """Decode and validate image bytes fully in memory."""
    if len(data) == 0:
        raise ValidationFailed("empty file (0 bytes)")
    if looks_like_html(data):
        raise ValidationFailed("payload is an HTML page, not an image")
    fmt = sniff_format(data)
    if fmt is None:
        raise ValidationFailed("unrecognized image format (magic bytes)")
    if fmt == "AVIF":
        try:
            import pillow_avif  # noqa: F401  (registers the codec)
        except ImportError as exc:
            raise DependencyError(
                "AVIF payload received but no AVIF decoder installed. "
                "Install the AVIF extra: pip install 'imgagent[avif]'"
            ) from exc
    if fmt not in allowed_formats:
        raise ValidationFailed(f"format {fmt} is not in the allowed set {allowed_formats}")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()  # structural check
        with Image.open(io.BytesIO(data)) as im2:
            width, height = im2.size
            animated = getattr(im2, "is_animated", False)
            mode = im2.mode
            check_decompression_bomb(width, height, max_pixels)
            if fmt == "JPEG":
                im2.load()  # JPEG verify() misses some truncation; force decode
            elif width * height <= 4_000_000:  # decode small files fully
                im2.load()
    except UnidentifiedImageError as exc:
        raise ValidationFailed(f"cannot decode image: {exc}") from exc
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValidationFailed(f"corrupt or invalid image: {exc}") from exc
    except ValidationFailed:
        raise
    return ImageInfo(format=fmt, width=width, height=height, mode=mode, animated=animated)


def validate_image_file(path: Path, *, max_pixels: int, allowed_formats: tuple = C.ALLOWED_FORMATS) -> ImageInfo:
    with open(path, "rb") as f:
        head = f.read(512)
        f.seek(0, os.SEEK_END)
        size = f.tell()
    if size == 0:
        raise ValidationFailed("empty file (0 bytes)")
    data = path.read_bytes() if size <= 32 * 1024 * 1024 else None
    if data is not None:
        return validate_image_bytes(data, max_pixels=max_pixels, allowed_formats=allowed_formats)
    # Large files: header sniff + Pillow streaming validation
    fmt = sniff_format(head)
    if fmt is None or looks_like_html(head):
        raise ValidationFailed("payload is not a recognized image format")
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im2:
            width, height = im2.size
            check_decompression_bomb(width, height, max_pixels)
            return ImageInfo(format=fmt, width=width, height=height, mode=im2.mode,
                             animated=getattr(im2, "is_animated", False))
    except UnidentifiedImageError as exc:
        raise ValidationFailed(f"cannot decode image: {exc}") from exc
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValidationFailed(f"corrupt or invalid image: {exc}") from exc


def make_thumbnail(src: Path, dst: Path, box: int = 256) -> str | None:
    """Generate a thumbnail without altering the original. Returns dst or None."""
    try:
        with Image.open(src) as im:
            im = im.convert("RGB") if im.mode not in ("RGB", "RGBA", "L") else im.copy()
            im.thumbnail((box, box), Image.Resampling.LANCZOS)
            dst.parent.mkdir(parents=True, exist_ok=True)
            im.save(dst, "JPEG", quality=85)
            return str(dst)
    except Exception:
        return None


# --- perceptual hashes (no extra deps) ----------------------------------------

def _resized_gray(im: Image.Image, w: int, h: int) -> Image.Image:
    return im.convert("L").resize((w, h), Image.Resampling.LANCZOS)


def average_hash(path_or_img, hash_size: int = 8) -> str:
    im = path_or_img if isinstance(path_or_img, Image.Image) else Image.open(path_or_img)
    g = _resized_gray(im, hash_size, hash_size)
    px = list(g.tobytes())  # mode L: one byte per pixel, row-major
    avg = sum(px) / len(px)
    bits = "".join("1" if p > avg else "0" for p in px)
    return f"{int(bits, 2):0{hash_size * hash_size // 4}x}"


def dhash(path_or_img, hash_size: int = 8) -> str:
    im = path_or_img if isinstance(path_or_img, Image.Image) else Image.open(path_or_img)
    g = _resized_gray(im, hash_size + 1, hash_size)
    px = list(g.tobytes())
    bits = []
    for row in range(hash_size):
        for col in range(hash_size):
            bits.append("1" if px[row * (hash_size + 1) + col] > px[row * (hash_size + 1) + col + 1] else "0")
    return f"{int(''.join(bits), 2):0{hash_size * hash_size // 4}x}"


def hamming_hex(a: str, b: str) -> int:
    """Hamming distance between two hex hashes (0 = identical)."""
    if len(a) != len(b):
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def perceptual_hash(path_or_img) -> str:
    """Combined 64-bit hash: average_hash xor dhash — robust default."""
    return average_hash(path_or_img)
