"""
Visual Ruler Grid — draw calibration markers on a screenshot.

When Kim needs to click something on the desktop, we draw a subtle grid of
labeled cross-markers ("A1", "B5", "J10") onto the screenshot image before
sending it to the LLM.  The LLM uses these markers as visual rulers to
calculate the exact (X, Y) pixel coordinate of its target.

The markers are drawn on the IMAGE ONLY — the user never sees them on their
actual screen.  A coordinate mapping is returned alongside the annotated image
so the agent loop can translate marker-relative positions into real pixel
coordinates (accounting for screenshot scaling).
"""

import logging
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Column labels: A–J (10 columns)
_COL_LABELS = "ABCDEFGHIJ"
# Row labels: 1–10
_ROW_COUNT = 10

# Marker appearance
_CROSS_ARM = 10          # pixels — half-length of each cross arm
_CROSS_WIDTH = 2         # stroke width of the cross
_LABEL_OFFSET_X = 6      # px offset from cross centre to label
_LABEL_OFFSET_Y = -14    # px offset (negative = above cross)
_MARKER_COLOR = (0, 255, 100)       # bright green — visible on most backgrounds
_OUTLINE_COLOR = (0, 0, 0)          # black outline for contrast
_LABEL_COLOR = (0, 255, 100)        # same green for labels
_LABEL_OUTLINE_COLOR = (0, 0, 0)    # black outline for label text
_INSET_RATIO = 0.04                 # 4% inset from edges so markers don't clip


def annotate_screenshot(
    img: Image.Image,
    grid_cols: int = 10,
    grid_rows: int = 10,
    original_width: Optional[int] = None,
    original_height: Optional[int] = None,
) -> tuple[Image.Image, dict]:
    """
    Draw a ruler grid onto a screenshot and return the annotated image
    plus a coordinate mapping.

    Parameters
    ----------
    img : PIL.Image
        The screenshot image to annotate (may already be scaled down).
    grid_cols : int
        Number of columns in the grid (default 10, labels A–J).
    grid_rows : int
        Number of rows in the grid (default 10, labels 1–10).
    original_width : int, optional
        The real screen width before scaling.  If provided, the returned
        coordinate mapping uses real-screen pixel values so pyautogui
        clicks land correctly.  If None, mapping uses image coordinates.
    original_height : int, optional
        The real screen height before scaling.

    Returns
    -------
    (annotated_img, grid_map)
        annotated_img : PIL.Image — a copy of `img` with grid markers drawn.
        grid_map : dict — ``{"A1": [real_x, real_y], "B3": [real_x, real_y], ...}``
            Pixel coordinates of each marker in **real screen space** (if
            original_width/height were provided) or image space otherwise.
    """
    # Work on a copy so the caller keeps the original
    annotated = img.copy()
    draw = ImageDraw.Draw(annotated)

    img_w, img_h = annotated.size

    # Scale factors: image → real screen
    sx = (original_width / img_w) if original_width else 1.0
    sy = (original_height / img_h) if original_height else 1.0

    # Inset the grid so edge markers aren't clipped
    inset_x = int(img_w * _INSET_RATIO)
    inset_y = int(img_h * _INSET_RATIO)
    usable_w = img_w - 2 * inset_x
    usable_h = img_h - 2 * inset_y

    # Column and row labels
    col_labels = list(_COL_LABELS[:grid_cols])
    row_labels = [str(i) for i in range(1, grid_rows + 1)]

    # Spacing between markers
    col_step = usable_w / max(grid_cols - 1, 1)
    row_step = usable_h / max(grid_rows - 1, 1)

    # Try to load a small font; fall back to default if unavailable
    font = _load_font(size=12)

    grid_map: dict[str, list[int]] = {}

    for ci, col_label in enumerate(col_labels):
        for ri, row_label in enumerate(row_labels):
            # Position in image coordinates
            ix = int(inset_x + ci * col_step)
            iy = int(inset_y + ri * row_step)

            # Draw the cross marker with outline for contrast
            _draw_cross(draw, ix, iy)

            # Draw the label (e.g. "A1") next to the cross
            label = f"{col_label}{row_label}"
            _draw_label(draw, ix, iy, label, font)

            # Map to real screen coordinates
            real_x = int(ix * sx)
            real_y = int(iy * sy)
            grid_map[label] = [real_x, real_y]

    logger.info(
        f"Annotated screenshot {img_w}×{img_h} with "
        f"{grid_cols}×{grid_rows} grid ({len(grid_map)} markers), "
        f"scale=({sx:.2f}, {sy:.2f})"
    )

    return annotated, grid_map


def _draw_cross(draw: ImageDraw.ImageDraw, cx: int, cy: int) -> None:
    """Draw a small + cross with a black outline for contrast."""
    # Black outline (drawn slightly thicker behind the coloured cross)
    for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        # Horizontal arm outline
        draw.line(
            [(cx - _CROSS_ARM + dx, cy + dy), (cx + _CROSS_ARM + dx, cy + dy)],
            fill=_OUTLINE_COLOR,
            width=_CROSS_WIDTH + 2,
        )
        # Vertical arm outline
        draw.line(
            [(cx + dx, cy - _CROSS_ARM + dy), (cx + dx, cy + _CROSS_ARM + dy)],
            fill=_OUTLINE_COLOR,
            width=_CROSS_WIDTH + 2,
        )

    # Coloured cross on top
    draw.line(
        [(cx - _CROSS_ARM, cy), (cx + _CROSS_ARM, cy)],
        fill=_MARKER_COLOR,
        width=_CROSS_WIDTH,
    )
    draw.line(
        [(cx, cy - _CROSS_ARM), (cx, cy + _CROSS_ARM)],
        fill=_MARKER_COLOR,
        width=_CROSS_WIDTH,
    )


def _draw_label(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    label: str,
    font: Optional[ImageFont.FreeTypeFont],
) -> None:
    """Draw a label string near the cross marker with a black text outline."""
    lx = cx + _LABEL_OFFSET_X
    ly = cy + _LABEL_OFFSET_Y

    # Black outline: draw the label shifted by 1px in each direction
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            draw.text(
                (lx + dx, ly + dy),
                label,
                fill=_LABEL_OUTLINE_COLOR,
                font=font,
            )

    # Main coloured label
    draw.text((lx, ly), label, fill=_LABEL_COLOR, font=font)


def _load_font(size: int = 12) -> Optional[ImageFont.FreeTypeFont]:
    """Try to load a readable monospace/sans-serif font at the given size."""
    # Common font paths on macOS, Linux, and Windows
    _FONT_CANDIDATES = [
        # macOS
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        # Windows
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue

    # Fallback: Pillow's built-in bitmap font (tiny but always available)
    logger.debug("No TrueType font found; using Pillow default font")
    return ImageFont.load_default()
