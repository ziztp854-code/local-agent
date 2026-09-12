import base64
import stat
from pathlib import Path


MAX_IMAGES = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024


def _mime_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Unsupported image format")


def _read_image(raw_path):
    try:
        path = Path(raw_path)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Image path must be a regular file")
        with path.open("rb") as source:
            data = source.read(MAX_IMAGE_BYTES + 1)
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("Image path must be a readable file") from error
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds the 10 MB limit")
    return data, _mime_type(data)


def build_user_message(prompt, paths):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Prompt cannot be empty")
    try:
        paths = list(paths)
    except TypeError as error:
        raise ValueError("Image paths must be iterable") from error
    if len(paths) > MAX_IMAGES:
        raise ValueError("At most four images are allowed")

    plain_message = {"role": "user", "content": prompt}
    if not paths:
        return plain_message, plain_message.copy()

    images = []
    total_bytes = 0
    for raw_path in paths:
        data, mime_type = _read_image(raw_path)
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("Images exceed the 20 MiB request limit")
        images.append((data, mime_type))

    content = [{"type": "text", "text": prompt}]
    for data, mime_type in images:
        encoded = base64.b64encode(data).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}}
        )

    wire_message = {"role": "user", "content": content}
    stored_message = {
        "role": "user",
        "content": f"{prompt}\n\n[Attached images: {len(images)}]",
    }
    return wire_message, stored_message
