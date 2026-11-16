"""Status images, so a still never passes for live video. Authored-by: CPCS.

Blocking (Pillow); call through hass.async_add_executor_job.
"""

from __future__ import annotations

import io
import textwrap

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 640, 360
MAX_LINES = 11
WRAP_CHARS = 52


def render(lines: list[str], frame: bytes | None = None) -> bytes:
    """A JPEG: the last frame darkened with a banner, or a dark card."""
    if frame is not None:
        try:
            img = Image.open(io.BytesIO(frame)).convert("RGB").resize((WIDTH, HEIGHT))
            img = Image.eval(img, lambda v: v * 45 // 100)
        except (OSError, ValueError):
            img = Image.new("RGB", (WIDTH, HEIGHT), (16, 16, 20))
    else:
        img = Image.new("RGB", (WIDTH, HEIGHT), (16, 16, 20))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=20)
    wrapped: list[str] = []
    for text in lines:
        wrapped.extend(textwrap.wrap(text, WRAP_CHARS) or [""])
    y = 20
    for text in wrapped[:MAX_LINES]:
        draw.text((24, y), text, fill=(230, 230, 230), font=font)
        y += 30
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=80)
    return out.getvalue()
