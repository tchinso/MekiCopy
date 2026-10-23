import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import mekicopy_settings as settings


class DetachedGeometryTests(unittest.TestCase):
    def test_signed_monitor_coordinates_are_parsed(self):
        for geometry in ("260x160", "260x160+-1920+-100", "260x160-1920+20"):
            self.assertEqual(settings._geometry_size(geometry), (260, 160))

    def test_corrupt_saved_geometry_uses_validated_fallback(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "geometry.json"
            for value in ("bad", "0x160+0+0", "260x160+oops+0", None):
                path.write_text(json.dumps({"geometry": value}), encoding="utf-8")
                with mock.patch.object(settings, "DETACHED_GEOMETRY_FILE", str(path)):
                    self.assertEqual(settings.load_detached_geometry("also bad"),
                                     settings.DETACHED_DEFAULT_GEOMETRY)
