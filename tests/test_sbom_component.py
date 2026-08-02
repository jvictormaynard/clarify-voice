import json
import tempfile
import unittest
from pathlib import Path

from scripts.add_sbom_component import SOX_COMPONENT, add_component


class SbomComponentTests(unittest.TestCase):
    def test_adds_verified_sox_component(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bom.json"
            path.write_text(json.dumps({"components": []}), encoding="utf-8")
            add_component(path)
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["components"], [SOX_COMPONENT])

    def test_rejects_malformed_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bom.json"
            path.write_text(json.dumps({"components": {}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "components list"):
                add_component(path)


if __name__ == "__main__":
    unittest.main()
