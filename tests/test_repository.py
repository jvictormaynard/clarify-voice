import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositorySafetyTests(unittest.TestCase):
    def test_windows_builds_never_bundle_dotenv(self):
        scripts = [
            ROOT / "scripts" / "build.ps1",
            ROOT / "scripts" / "deploy.ps1",
            ROOT / "build.bat",
        ]
        for script in scripts:
            content = script.read_text(encoding="utf-8")
            self.assertNotIn('"--add-data", "${envFile};."', content)
            self.assertNotIn('--add-data ".env;."', content)

    def test_deploy_stages_all_python_modules(self):
        content = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
        self.assertIn('Join-Path $repoRoot "*.py"', content)
        self.assertNotIn('Join-Path $soxDir "*.txt"', content)
        self.assertNotIn('Join-Path $soxDir "LICENSE.GPL.txt"', content)

    def test_deploy_uses_isolated_repository_requirements(self):
        content = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
        self.assertIn('$venvDir = Join-Path $buildRoot "venv"', content)
        self.assertIn(
            '$requirementsFile = Join-Path $requirementsDir '
            '"requirements-dev.txt"',
            content,
        )
        self.assertIn('"--upgrade", "-r", $requirementsFile', content)
        self.assertIn("Build dependencies: $dependencyVersions", content)
        self.assertNotIn(
            '"requests", "sounddevice", "customtkinter", "Pillow", '
            '"pyinstaller"',
            content,
        )

    def test_release_publishes_verified_sox_source(self):
        content = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("sox-14.4.2-source.tar.gz", content)
        self.assertIn(
            "b45f598643ffbd8e363ff24d61166ccec4836fea6d3888881b8df53e3bb55f6c",
            content,
        )

    def test_package_scripts_are_documented_maintainer_aliases(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertTrue(package["private"])
        self.assertEqual(
            set(package["scripts"]),
            {"test", "check", "build", "setup", "deploy"},
        )

    def test_open_source_community_files_exist(self):
        required = [
            "LICENSE",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "SECURITY.md",
            "SUPPORT.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
        ]
        for relative_path in required:
            self.assertTrue((ROOT / relative_path).is_file(), relative_path)


if __name__ == "__main__":
    unittest.main()
