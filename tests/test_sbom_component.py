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
            expected = {
                path.name: sha256(path.read_bytes()).hexdigest()
                for path in runtime_files(runtime_root, manifest)
            }
            file_components = {
                item["name"]: item
                for item in document["components"]
                if item.get("properties")
                and any(
                    prop.get("name") == "clarifyvoice:bundled-by"
                    for prop in item["properties"]
                )
            }
            self.assertEqual(set(file_components), set(expected))
            for name, digest in expected.items():
                self.assertEqual(
                    file_components[name]["hashes"],
                    [{"alg": "SHA-256", "content": digest}],
                )
                self.assertEqual(
                    file_components[name]["version"], "bundled-with-sox-14.4.2"
                )
                self.assertIn("licenses", file_components[name])
                self.assertIn(
                    "SoX distribution bundle", file_components[name]["description"]
                )
            dependency = next(
                item
                for item in document["dependencies"]
                if item["ref"] == "pkg:generic/sox@14.4.2"
            )
            self.assertEqual(
                set(dependency["dependsOn"]),
                {item["bom-ref"] for item in file_components.values()},
            )

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
