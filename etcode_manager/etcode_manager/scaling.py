"""Screen resampling and pixel-aligned, high-DPI-aware fit geometry.

QPainter's SmoothPixmapTransform is bilinear sampling, which aliases thin
strokes when a desktop is reduced by more than 2x.  Pre-resample the whole
frame instead: Qt's area sampler for speed, or Pillow's antialiased Lanczos-3
for sharper text.  The caller caches the result and paints it at device size.
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QImage


class ScalingFilter(str, Enum):
    AREA = "area"
    LANCZOS = "lanczos"


def fitted_rect(source: QSize, viewport: QSize, dpr: float) -> QRect:
    """Fit a source into a logical viewport; return an integer *device-pixel* rect.

    Round only in device space, including the letterbox offset, so painting
    never introduces a fractional-pixel translation and a second resampling.
    The final width and height may differ from the exact aspect ratio by a
    fraction of a pixel; input mapping must use these actual dimensions.
    """
    if dpr <= 0:
        raise ValueError("The device pixel ratio must be positive.")
    if source.isEmpty() or viewport.isEmpty():
        return QRect()
    width = max(1, int(viewport.width() * dpr + 0.5))
    height = max(1, int(viewport.height() * dpr + 0.5))
    scale = min(width / source.width(), height / source.height())
    w = max(1, min(width, int(source.width() * scale + 0.5)))
    h = max(1, min(height, int(source.height() * scale + 0.5)))
    return QRect((width - w) // 2, (height - h) // 2, w, h)


def resample_frame(image: QImage, size: QSize, filter: ScalingFilter) -> QImage:
    """Resize an opaque framebuffer to an exact physical size; the caller sets DPR.

    Return an implicitly shared copy at native size: neither filter should
    touch pixels when no resizing is necessary.  Always start from the original
    framebuffer, not a previously resized image.
    """
    filter = ScalingFilter(filter)
    if image.isNull():
        return QImage()
    if size.isEmpty():
        raise ValueError("The target image size must be positive.")
    if image.size() == size:
        return QImage(image)
    if filter == ScalingFilter.AREA:
        # Unlike QPainter's bilinear hint, QImage's reduction path integrates
        # the source pixel area, even at reductions below 50%.
        return image.scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)

    from PIL import Image

    # RGBX8888 is byte-ordered on every platform.  RGB32's native-endian BGRX
    # layout is not.  The local QImage keeps this borrowed memory alive until
    # Pillow has finished reading it; no PNG encoding or Python pixel loops.
    rgb = image.convertToFormat(QImage.Format.Format_RGBX8888)
    source = Image.frombuffer("RGBX", (rgb.width(), rgb.height()), rgb.constBits(),
                              "raw", "RGBX", rgb.bytesPerLine(), 1).convert("RGB")
    # Pillow's three-channel convolution is faster than processing RGBX's unused
    # fourth channel. Restore the Qt-friendly layout only at the output size.
    resized = source.resize((size.width(), size.height()), Image.Resampling.LANCZOS).convert("RGBX")
    # Detach from Pillow's temporary byte buffer before returning to the widget.
    return QImage(resized.tobytes(), size.width(), size.height(), size.width() * 4,
                  QImage.Format.Format_RGBX8888).copy()
