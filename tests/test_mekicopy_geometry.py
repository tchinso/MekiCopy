import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import mekicopy_settings as settings
import mekicopy_capture as capture
from PIL import Image


class CaptureFailureTests(unittest.TestCase):
    def test_strategy_dimension_mismatch_is_not_reported_as_a_black_frame(self):
        with mock.patch.object(capture.CaptureManager, "_read_monitors", return_value=[]):
            manager = capture.CaptureManager()
            with mock.patch.object(manager, "_capture_with_strategy", return_value=Image.new("RGB", (12, 11), "white")):
                result = manager.capture_region(capture.Region(0, 0, 12, 12))
        self.assertEqual(result.status, capture.CaptureStatus.SIZE_MISMATCH)
        self.assertEqual(manager.blank_frame_count, 0)
        self.assertEqual(result.stats.height, 11)


class DetachedGeometryTests(unittest.TestCase):
    def test_signed_monitor_coordinates_are_parsed(self):
        cases = {
            "260x160": (120, 120),
            "260x160+-1920+-100": (-1920, -100),
            "260x160-1920+20": (-1920, 20),
        }
        for geometry, (left, top) in cases.items():
            self.assertEqual(settings._geometry_size(geometry), (260, 160))
            self.assertEqual(settings.parse_detached_geometry(geometry), (260, 160, left, top))

    def test_corrupt_saved_geometry_uses_validated_fallback(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "geometry.json"
            for value in (
                "bad", "0x160+0+0", "260x160+oops+0", "260x160--1920+20",
                "260x160+99999999999999999999999+0", "99999999999999x160+0+0", None,
            ):
                path.write_text(json.dumps({"geometry": value}), encoding="utf-8")
                with mock.patch.object(settings, "DETACHED_GEOMETRY_FILE", str(path)):
                    self.assertEqual(settings.load_detached_geometry("also bad"),
                                     settings.DETACHED_DEFAULT_GEOMETRY)
