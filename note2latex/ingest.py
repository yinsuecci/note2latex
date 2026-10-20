import base64
import io
import warnings
from pathlib import Path

from PIL import Image, ImageOps

MAX_BYTES = 25 * 1024 * 1024
MAX_PAGES = 12
Image.MAX_IMAGE_PIXELS = 25_000_000


def normalized_image(image):
    image = ImageOps.exif_transpose(image)
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        image = Image.alpha_composite(background, rgba)
    image = image.convert("RGB")
    image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def ingest(name: str, data: bytes) -> list[bytes]:
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Each file must be 1 byte to 25 MB")
    if Path(name).suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        try:
            doc = pdfium.PdfDocument(data)
            with doc:
                if not 1 <= len(doc) <= MAX_PAGES:
                    raise ValueError("PDF must have 1 to 12 pages")
                pages = []
                for i in range(len(doc)):
                    page = doc[i]
                    width, height = page.get_size()
                    if min(width, height) <= 0:
                        raise ValueError("Invalid PDF dimensions")
                    bitmap = page.render(scale=min(2, 2400 / max(width, height)))
                    try:
                        pages.append(normalized_image(bitmap.to_pil()))
                    finally:
                        bitmap.close()
                        page.close()
                return pages
        except Exception:
            raise ValueError("Invalid, encrypted or oversized PDF") from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("Use JPEG, PNG, WebP or PDF")
                return [normalized_image(image)]
    except Exception:
        raise ValueError("Invalid image; use JPEG, PNG or WebP (max 25 megapixels)") from None


def image_content(path: Path):
    return {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode(),
            "detail": "high",
        },
    }
