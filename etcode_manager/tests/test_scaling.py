"""Screen filter, cache, input geometry and actual high-DPI rendering regressions."""

import gc
import os
from pathlib import Path
import statistics
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from etcode_manager.scaling import ScalingFilter, fitted_rect, resample_frame
from etcode_manager.view import VncView


def stripes(width=1920, height=24):
    """One-pixel strokes, reduced by >2x in the regression tests."""
    row = (b"\x00\x00\x00\xff\xff\xff\xff\xff" * ((width + 1) // 2))[:width * 4]
    return QImage(row * height, width, height, width * 4, QImage.Format.Format_RGB32).copy()


def pixels(image):
    # Ignore format, DPR and DPI metadata when comparing what is actually displayed.
    rgb = image.convertToFormat(QImage.Format.Format_RGB32)
    return bytes(rgb.constBits())


class ScalingTest(unittest.TestCase):
    def test_fit_is_device_aligned_centered_and_aspect_preserving(self):
        for dpr in (1.0, 1.25, 1.5, 2.0):
            for viewport in (QSize(641, 401), QSize(800, 900)):
                with self.subTest(dpr=dpr, viewport=viewport):
                    rect = fitted_rect(QSize(1920, 1080), viewport, dpr)
                    width = int(viewport.width() * dpr + 0.5)
                    height = int(viewport.height() * dpr + 0.5)
                    self.assertGreater(rect.width(), 0)
                    self.assertGreater(rect.height(), 0)
                    self.assertLessEqual(rect.x() + rect.width(), width)
                    self.assertLessEqual(rect.y() + rect.height(), height)
                    self.assertLessEqual(abs(2 * rect.x() + rect.width() - width), 1)
                    self.assertLessEqual(abs(2 * rect.y() + rect.height() - height), 1)
                    self.assertLessEqual(abs(rect.width() * 1080 - rect.height() * 1920), 1920)

    def test_empty_and_extreme_sizes(self):
        self.assertTrue(fitted_rect(QSize(), QSize(640, 400), 1).isEmpty())
        self.assertTrue(fitted_rect(QSize(1920, 1080), QSize(0, 400), 1).isEmpty())
        self.assertEqual(fitted_rect(QSize(1, 10000), QSize(640, 400), 1).size(), QSize(1, 400))
        with self.assertRaises(ValueError):
            fitted_rect(QSize(10, 10), QSize(640, 400), 0)
        with self.assertRaises(ValueError):
            resample_frame(stripes(), QSize(0, 0), ScalingFilter.AREA)
        self.assertTrue(resample_frame(QImage(), QSize(640, 400), ScalingFilter.AREA).isNull())

    def test_native_size_is_untouched_for_both_filters(self):
        image = stripes()
        with patch.dict(sys.modules, {"PIL": None}):
            for filter in ScalingFilter:
                with self.subTest(filter=filter):
                    result = resample_frame(image, image.size(), filter)
                    self.assertEqual(result.cacheKey(), image.cacheKey())
                    self.assertEqual(pixels(result), pixels(image))

    def test_area_matches_qt_resampling_at_several_scales_without_pillow(self):
        image = stripes()
        with patch.dict(sys.modules, {"PIL": None}):
            for size in (QSize(640, 8), QSize(808, 10), QSize(1440, 18), QSize(2400, 30)):
                with self.subTest(size=size):
                    expected = image.scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation)
                    actual = resample_frame(image, size, ScalingFilter.AREA)
                    self.assertEqual(actual.size(), size)
                    self.assertEqual(pixels(actual), pixels(expected))

    def test_area_reduces_aliasing_below_half_size(self):
        image = stripes()
        target = QSize(808, 10)
        bilinear = QImage(target, QImage.Format.Format_RGB32)
        painter = QPainter(bilinear)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(QRectF(0, 0, target.width(), target.height()), image)
        painter.end()
        area = resample_frame(image, target, ScalingFilter.AREA)

        def variation(frame):
            values = [frame.pixelColor(x, 5).red() for x in range(8, frame.width() - 8)]
            return statistics.pstdev(values)

        # Proper integration approaches a uniform grey; bilinear sampling
        # invents a high-contrast, low-frequency stripe pattern instead.
        self.assertLess(variation(area), variation(bilinear) * 0.5)
        self.assertLess(variation(area), 25)

    def test_lanczos_matches_pillow_and_preserves_colour_and_buffer_lifetime(self):
        from PIL import Image

        # RGB888 with a padded stride, plus unequal R/G/B values, catches both
        # assumptions about row packing and accidental BGR/RGB swaps.
        image = QImage(67, 31, QImage.Format.Format_RGB888)
        image.fill(QColor(23, 101, 207))
        painter = QPainter(image)
        painter.fillRect(13, 2, 19, 20, QColor(210, 31, 81))
        painter.fillRect(36, 8, 20, 12, QColor(40, 220, 90))
        painter.end()
        reference = Image.frombytes("RGB", (67, 31), bytes(image.constBits()),
                                    "raw", "RGB", image.bytesPerLine(), 1)
        for size in (QSize(23, 11), QSize(47, 22), QSize(91, 42)):
            with self.subTest(size=size):
                actual = resample_frame(image, size, ScalingFilter.LANCZOS)
                expected = reference.resize((size.width(), size.height()), Image.Resampling.LANCZOS)
                gc.collect()  # Pillow's temporary output buffer must not be borrowed by QImage.
                for y in range(size.height()):
                    for x in range(size.width()):
                        self.assertEqual(actual.pixelColor(x, y).getRgb(), (*expected.getpixel((x, y)), 255))

    def test_lanczos_is_not_an_alias_for_area_scaling(self):
        image = QImage(128, 64, QImage.Format.Format_RGB32)
        image.fill(QColor(64, 64, 64))
        painter = QPainter(image)
        painter.fillRect(64, 0, 64, 64, QColor(192, 192, 192))
        painter.end()
        size = QSize(53, 27)
        area = resample_frame(image, size, ScalingFilter.AREA)
        sharp = resample_frame(image, size, ScalingFilter.LANCZOS)
        self.assertNotEqual(pixels(area), pixels(sharp))
        # Lanczos has negative lobes: unlike an area average, it can slightly
        # overshoot. This is why it is an opt-in, not an unconditional upgrade.
        self.assertLess(min(sharp.pixelColor(x, 13).red() for x in range(53)), 64)


class ViewScalingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_view(self):
        view = VncView()
        view.resize(808, 600)
        image = QImage(1920, 1080, QImage.Format.Format_RGB32)
        image.fill(QColor(23, 101, 207))
        view._set_image(image)
        self.addCleanup(view.close)
        return view

    def test_repaints_reuse_cache_and_rebuild_on_frame_size_and_filter_changes(self):
        view = self.make_view()
        with patch("etcode_manager.view.resample_frame", wraps=resample_frame) as resize:
            view.grab()
            view.grab()
            self.assertEqual(resize.call_count, 1)

            image = QImage(view._image)
            image.fill(QColor(50, 160, 90))
            view._set_image(image)
            view.grab()
            self.assertEqual(resize.call_count, 2)
            self.assertEqual(view._scaled_image.pixelColor(0, 0), QColor(50, 160, 90))

            view.resize(810, 601)
            view.grab()
            self.assertEqual(resize.call_count, 3)

            view.set_scaling_filter(ScalingFilter.LANCZOS)
            view.grab()
            self.assertEqual(resize.call_count, 4)
            view.set_scaling_filter("lanczos")
            view.grab()
            self.assertEqual(resize.call_count, 4)

            # A guest resolution change, not just different pixels of the same size.
            image = QImage(1280, 720, QImage.Format.Format_RGB32)
            image.fill(Qt.GlobalColor.red)
            view._set_image(image)
            view.grab()
            self.assertEqual(resize.call_count, 5)

    def test_dpr_change_invalidates_cache_without_modifying_original(self):
        view = self.make_view()
        size = QSize(808, 455)
        with patch("etcode_manager.view.resample_frame", wraps=resample_frame) as resize:
            first = view._display_image(size, 1.0)
            self.assertIs(first, view._display_image(size, 1.0))
            second = view._display_image(size, 2.0)
            self.assertEqual(resize.call_count, 2)
            self.assertEqual(first.devicePixelRatio(), 1.0)
            self.assertEqual(second.devicePixelRatio(), 2.0)
            self.assertEqual(view._image.devicePixelRatio(), 1.0)
        # Native-size caching must also leave the source DPR unchanged.
        view._display_image(view._image.size(), 1.5)
        self.assertEqual(view._image.devicePixelRatio(), 1.0)

    def test_disconnect_and_null_frames_release_cache(self):
        view = self.make_view()
        view.grab()
        self.assertIsNotNone(view._scaled_image)
        view.disconnect_from()
        self.assertIsNone(view._image)
        self.assertIsNone(view._scaled_image)
        self.assertIsNone(view._scaled_key)
        view._set_image(QImage())
        view.grab()  # no divide by zero or stale framebuffer
        self.assertEqual(view._to_screen(QPointF(100, 100)), (0, 0))

    def test_input_uses_actual_rounded_fit_even_before_a_paint(self):
        view = self.make_view()
        for size in (QSize(641, 401), QSize(777, 801)):
            view.resize(size)
            for dpr in (1.0, 1.25, 1.5, 2.0):
                with self.subTest(size=size, dpr=dpr), patch.object(view, "devicePixelRatioF", return_value=dpr):
                    rect = fitted_rect(view._image.size(), view.size(), dpr)
                    for x, y in ((0, 0), (500, 600), (1919, 1079)):
                        position = QPointF((rect.x() + (x + 0.25) * rect.width() / 1920) / dpr,
                                           (rect.y() + (y + 0.25) * rect.height() / 1080) / dpr)
                        self.assertEqual(view._to_screen(position), (x, y))
                    self.assertEqual(view._to_screen(QPointF(-10, -10)), (0, 0))
                    self.assertEqual(view._to_screen(QPointF(10000, 10000)), (1919, 1079))

    def test_real_high_dpi_paint_is_exactly_one_resampling(self):
        # Qt establishes process DPI before QApplication is constructed. Use
        # real subprocess scale factors, not a mocked DPR on a 1x paint device.
        script = textwrap.dedent("""\
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QColor, QFont, QImage, QPainter
            from PySide6.QtWidgets import QApplication
            from etcode_manager.scaling import ScalingFilter, fitted_rect, resample_frame
            from etcode_manager.view import VncView

            app = QApplication([])
            image = QImage(1920, 1080, QImage.Format.Format_RGB32)
            image.fill(QColor(25, 30, 40))
            painter = QPainter(image)
            font = QFont('monospace')
            font.setPixelSize(16)
            painter.setFont(font)
            painter.setPen(QColor(220, 230, 240))
            for y in range(24, 1080, 24):
                painter.drawText(13, y, 'def render(frame): return frame  # 0123456789 ' * 4)
            painter.end()
            view = VncView()
            view.resize(641, 401)  # odd dimensions exercise fractional-DPI rounding
            view._set_image(image)
            view.show()
            app.processEvents()
            dpr = view.devicePixelRatioF()
            for filter in ScalingFilter:
                view.set_scaling_filter(filter)
                captured = view.grab().toImage()
                rect = fitted_rect(image.size(), view.size(), dpr)
                expected = resample_frame(image, rect.size(), filter)
                actual = captured.copy(rect)
                fmt = QImage.Format.Format_RGB32
                actual_rgb = actual.convertToFormat(fmt)
                expected_rgb = expected.convertToFormat(fmt)
                assert bytes(actual_rgb.constBits()) == bytes(expected_rgb.constBits()), (dpr, filter)
                assert view._scaled_image.size() == rect.size()
                assert view._scaled_image.devicePixelRatio() == dpr
                assert image.devicePixelRatio() == 1.0
                assert captured.pixelColor(0, 0) == QColor(0, 0, 0), 'letterbox must remain black'
            view.close()
            print(dpr)
        """)
        for factor in (1.0, 1.25, 1.5, 2.0):
            with self.subTest(dpr=factor):
                env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=str(factor),
                           QT_SCALE_FACTOR_ROUNDING_POLICY="PassThrough", PYTHONDONTWRITEBYTECODE="1")
                result = subprocess.run([sys.executable, "-c", script], env=env,
                                        cwd=Path(__file__).resolve().parents[1],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(float(result.stdout.strip()), factor)


if __name__ == "__main__":
    unittest.main()
