"""مولّد أيقونة التطبيق: يكتب assets/app_icon.ico و assets/app_icon.png بلا تبعيات.

التصميم يطابق هوية «الوكيل المحلي»: مربّع بزوايا دائرية بلون الحبر الداكن،
ومعيّن ◆ بلون الطين مع لمسة نحاسية، وحرف صغير من العلامة. يُرسَم بتنعيم حواف
عبر الإفراط في العيّنات (supersampling) ثم التصغير، ويُرمّز PNG يدويًا بـ zlib.

التشغيل:
    py -3.12 tools/make_icon.py
"""

import struct
import zlib
from pathlib import Path

# هوية «أسود بالزمرد»: مربّع أسود عميق، معيّن زمردي مشبع بتدرّج، ولمعة نعناع.
INK = (0x05, 0x09, 0x07)     # قاعدة سوداء عميقة
CLAY = (0x00, 0xF5, 0xA8)    # زمرد مشرق (أعلى المعيّن)
COPPER = (0x00, 0x9E, 0x6B)  # زمرد أعمق (أسفل المعيّن)
CREAM = (0xEA, 0xFF, 0xF6)   # نعناع فاتح للّب

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
SUPERSAMPLE = 6


def _rounded_rect_alpha(x, y, size, radius):
    """تغطية نقطة داخل مستطيل بزوايا دائرية (1 داخل، 0 خارج)."""
    inner_min = radius
    inner_max = size - radius
    cx = min(max(x, inner_min), inner_max)
    cy = min(max(y, inner_min), inner_max)
    dx = x - cx
    dy = y - cy
    if dx == 0 and dy == 0:
        return 1.0
    distance = (dx * dx + dy * dy) ** 0.5
    return 1.0 if distance <= radius else 0.0


def _in_diamond(x, y, size, half_w, half_h):
    cx = cy = size / 2
    if half_w <= 0 or half_h <= 0:
        return False
    return abs(x - cx) / half_w + abs(y - cy) / half_h <= 1.0


def _composite(base, over, alpha):
    return tuple(round(b + (o - b) * alpha) for b, o in zip(base, over))


def _render(size):
    """أعِد صورة RGBA (bytearray) بحجم size×size مع تنعيم الحواف."""
    big = size * SUPERSAMPLE
    radius = big * 0.24
    half_w = big * 0.30
    half_h = big * 0.38
    inner_w = big * 0.135
    inner_h = big * 0.17
    # لوحة فرعية بلون واحد لكل بكسل مكبّر، ثم نصغّرها بالمعدّل.
    pixels = [(0, 0, 0, 0)] * (big * big)
    for py in range(big):
        y = py + 0.5
        row = py * big
        for px in range(big):
            x = px + 0.5
            if not _rounded_rect_alpha(x, y, big, radius):
                continue
            color = INK
            if _in_diamond(x, y, big, half_w, half_h):
                # تدرّج قطري خفيف من الطين إلى النحاس داخل المعيّن.
                t = (x + y) / (2 * big)
                color = _composite(CLAY, COPPER, max(0.0, min(1.0, t)))
                if _in_diamond(x, y, big, inner_w, inner_h):
                    color = CREAM
            pixels[row + px] = (*color, 255)

    # تصغير بالمعدّل (box downsample) لإنتاج حواف ناعمة وشفافية جزئية.
    out = bytearray(size * size * 4)
    area = SUPERSAMPLE * SUPERSAMPLE
    for oy in range(size):
        for ox in range(size):
            r = g = b = a = 0
            for sy in range(SUPERSAMPLE):
                base = (oy * SUPERSAMPLE + sy) * big + ox * SUPERSAMPLE
                for sx in range(SUPERSAMPLE):
                    pr, pg, pb, pa = pixels[base + sx]
                    r += pr
                    g += pg
                    b += pb
                    a += pa
            index = (oy * size + ox) * 4
            out[index] = r // area
            out[index + 1] = g // area
            out[index + 2] = b // area
            out[index + 3] = a // area
    return out


def _png_bytes(rgba, size):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter type 0
        raw.extend(rgba[y * stride : (y + 1) * stride])
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _ico_bytes(images):
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    entries = bytearray()
    payload = bytearray()
    offset = 6 + count * 16
    for size, png in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        payload += png
        offset += len(png)
    return bytes(header + entries + payload)


def main():
    assets = Path(__file__).resolve().parent.parent / "assets"
    assets.mkdir(exist_ok=True)
    images = []
    for size in ICO_SIZES:
        png = _png_bytes(_render(size), size)
        images.append((size, png))
        if size == 256:
            (assets / "app_icon.png").write_bytes(png)
    (assets / "app_icon.ico").write_bytes(_ico_bytes(images))
    print(f"wrote {assets / 'app_icon.ico'} and {assets / 'app_icon.png'}")


if __name__ == "__main__":
    main()
