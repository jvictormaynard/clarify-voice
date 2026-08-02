import tempfile
import unittest
from pathlib import Path

from scripts.check_runtime_lock import _shared_pin_mismatches


class RuntimeLockTests(unittest.TestCase):
    def test_shared_runtime_pins_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / "build.txt"
            runtime = root / "runtime.txt"
            build.write_text("requests==2.34.2\nPillow==12.3.0\n", encoding="utf-8")
            runtime.write_text("requests==2.34.2\npillow==12.3.0\n", encoding="utf-8")
            self.assertEqual(_shared_pin_mismatches(build, runtime), {})

            runtime.write_text("requests==2.34.3\npillow==12.3.0\n", encoding="utf-8")
            self.assertEqual(
                _shared_pin_mismatches(build, runtime),
                {"requests": ("2.34.2", "2.34.3")},
            )

            build.write_text("requests==2.34.2\n", encoding="utf-8")
            self.assertEqual(
                _shared_pin_mismatches(build, runtime),
                {
                    "pillow": (None, "12.3.0"),
                    "requests": ("2.34.2", "2.34.3"),
                },
            )

    def test_malformed_markers_and_lines_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / "build.txt"
            runtime = root / "runtime.txt"
            build.write_text("requests==2.34.2 ;\n", encoding="utf-8")
            runtime.write_text("requests==2.34.2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "malformed lock pin"):
                _shared_pin_mismatches(build, runtime)

            build.write_text("requests 2.34.2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "malformed lock pin"):
                _shared_pin_mismatches(build, runtime)


if __name__ == "__main__":
    unittest.main()
