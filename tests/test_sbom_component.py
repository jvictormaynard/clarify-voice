import json
import tempfile
import unittest
from pathlib import Path
from hashlib import sha256

from scripts.add_sbom_component import (
    SOURCE_ARCHIVE_SHA256,
    add_component,
    runtime_files,
)


class SbomComponentTests(unittest.TestCase):
    def test_adds_hashes_for_the_files_build_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bom.json"
            path.write_text(json.dumps({"components": []}), encoding="utf-8")
            runtime_root = Path(__file__).parents[1] / "extra" / "sox-14.4.2"
            manifest = (
                Path(__file__).parents[1] / "scripts" / "sox-runtime-manifest.json"
            )
            add_component(path, runtime_root, manifest)
            document = json.loads(path.read_text(encoding="utf-8"))
            component = document["components"][0]
            self.assertNotIn("hashes", component)
            self.assertIn(
                SOURCE_ARCHIVE_SHA256, component["externalReferences"][0]["comment"]
            )
            properties = {
                item["name"].split(":")[-2]: item["value"]
                for item in component["properties"]
            }
            expected = {
                path.name: sha256(path.read_bytes()).hexdigest()
                for path in runtime_files(runtime_root, manifest)
            }
            self.assertEqual(properties, expected)

    def test_rejects_malformed_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bom.json"
            path.write_text(json.dumps({"components": {}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "components list"):
                add_component(
                    path,
                    Path(__file__).parents[1] / "extra" / "sox-14.4.2",
                    Path(__file__).parents[1] / "scripts" / "sox-runtime-manifest.json",
                )


if __name__ == "__main__":
    unittest.main()
