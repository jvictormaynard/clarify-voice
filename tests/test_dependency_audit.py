import unittest

from scripts.dependency_audit import _vulnerability_ids


class DependencyAuditFormatTests(unittest.TestCase):
    def test_current_object_root_fixture(self):
        payload = {
            "dependencies": [
                {"name": "example", "version": "1.0", "vulns": [{"id": "PYSEC-1"}]},
                {"name": "safe", "version": "2.0", "vulns": []},
            ],
            "fixes": [],
        }
        self.assertEqual(_vulnerability_ids(payload), {"PYSEC-1"})

    def test_legacy_list_root_fixture(self):
        payload = [{"name": "example", "version": "1.0", "vulns": [{"id": "PYSEC-2"}]}]
        self.assertEqual(_vulnerability_ids(payload), {"PYSEC-2"})

    def test_malformed_fixture_fails_closed(self):
        with self.assertRaises(ValueError):
            _vulnerability_ids({"dependencies": ["not-a-package"]})


if __name__ == "__main__":
    unittest.main()
