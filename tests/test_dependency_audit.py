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
        fixtures = (
            ({"fixes": []}, "dependencies"),
            ({"dependencies": ["not-a-package"]}, "package entries"),
            ({"dependencies": {"example": []}}, "dependencies must be a list"),
            ({"dependencies": [{"name": "example"}]}, "contain vulns"),
            (
                {"dependencies": [{"name": "example", "vulns": {}}]},
                "vulnerabilities must be a list",
            ),
        )
        for payload, message in fixtures:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                _vulnerability_ids(payload)


if __name__ == "__main__":
    unittest.main()
