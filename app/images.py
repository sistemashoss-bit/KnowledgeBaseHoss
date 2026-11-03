"""Optimización de imágenes subidas: se convierten a WebP antes de guardarlas.

- Solo imágenes rasterizadas fijas (JPEG, PNG, BMP, TIFF, WebP). GIF, SVG, HEIC,
  ICO y las animadas (APNG, WebP animado) se guardan tal cual.
- Respeta la orientación EXIF (si no, las fotos de celular quedan giradas) y
  descarta los metadatos (EXIF con GPS incluido).
- Reduce a MAX_SIDE px el lado mayor y conserva la transparencia.
- Si el WebP no sale más chico que el original (y no hubo reducción) o algo
  falla, se guarda el original: optimizar nunca debe impedir una subida.
"""
import logging
import os
from io import BytesIO

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

MAX_SIDE = 2560
QUALITY = 80
# Tope de píxeles a decodificar (≈ 8000 × 6000): más que eso se guarda sin tocar
# en vez de arriesgar memoria con una "bomba de descompresión".
MAX_PIXELS = 50_000_000

_CONVERTIBLE = {"JPEG", "MPO", "PNG", "BMP", "TIFF", "WEBP"}
_HAS_ALPHA_MODES = {"RGBA", "LA", "PA", "RGBa", "La"}


def optimize_image(content: bytes, filename: str, content_type: str | None) -> tuple[bytes, str, str]:
    """(contenido, nombre, content_type) ya en WebP, o los originales si no
    aplica. `filename` cambia de extensión a .webp."""
    is_image = (content_type or "").startswith("image/") or _looks_like_image(filename)
    if not content or not is_image:
        return content, filename, content_type
    try:
        with Image.open(BytesIO(content)) as img:
            if img.format not in _CONVERTIBLE or getattr(img, "n_frames", 1) > 1:
                return content, filename, content_type
            if img.width * img.height > MAX_PIXELS:
                return content, filename, content_type
            if img.mode == "F":
                return content, filename, content_type
            if img.mode.startswith("I"):
                # Gris de 16/32 bits: convert("RGB") recorta todo lo > 255 a blanco;
                # se escala a 8 bits primero.
                scale = 1 / 256 if img.mode.startswith("I;16") or img.getextrema()[1] > 255 else 1
                img = img.convert("I").point(lambda v: v * scale).convert("L")
            img = ImageOps.exif_transpose(img)
            resized = max(img.size) > MAX_SIDE
            if resized:
                img.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
            has_alpha = img.mode in _HAS_ALPHA_MODES or (img.mode == "P" and "transparency" in img.info)
            img = img.convert("RGBA" if has_alpha else "RGB")
            out = BytesIO()
            img.save(out, "WEBP", quality=QUALITY, method=4)
    except Exception:
        log.warning("No se pudo optimizar la imagen %r; se guarda la original", filename, exc_info=True)
        return content, filename, content_type

    webp = out.getvalue()
    if len(webp) >= len(content) and not resized:
        return content, filename, content_type
    return webp, _webp_name(filename), "image/webp"


def _looks_like_image(filename: str) -> bool:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


def _webp_name(filename: str) -> str:
    stem = os.path.splitext(filename or "")[0] or "imagen"
    return f"{stem}.webp"
