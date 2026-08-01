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
            '$requirementsFile = Join-Path $requirementsDir "requirements-dev.txt"',
            content,
        )
        self.assertIn('"-r", $requirementsFile, "-c", $lockFile', content)
        self.assertIn("Build dependencies: $dependencyVersions", content)
        self.assertNotIn(
            '"requests", "sounddevice", "customtkinter", "Pillow", "pyinstaller"',
            content,
        )

    def test_release_publishes_verified_sox_source(self):
        content = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
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

    def test_release_skill_is_repository_local_and_complete(self):
        skill_root = ROOT / ".agents" / "skills" / "clarifyvoice-release"
        required = [
            "SKILL.md",
            "agents/openai.yaml",
            "references/release-contract.md",
            "scripts/release_preflight.py",
        ]
        for relative_path in required:
            self.assertTrue((skill_root / relative_path).is_file(), relative_path)

        skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: clarifyvoice-release", skill)
        self.assertIn("ClarifyVoice.exe.sha256", skill)
        self.assertNotIn("/mnt/c/Users/Work/.codex/skills", skill)

    def test_dependency_contract_files_exist_and_have_review_policy(self):
        lock = ROOT / "requirements-lock.txt"
        self.assertTrue(lock.is_file())
        self.assertIn("pip-compile", lock.read_text(encoding="utf-8"))

        policy = json.loads(
            (ROOT / "dependency-audit.json").read_text(encoding="utf-8")
        )
        self.assertEqual(policy["policy_version"], 1)
        self.assertIsInstance(policy["ignored_vulnerabilities"], dict)

        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[tool.ruff]", pyproject)
        self.assertIn("[tool.mypy]", pyproject)
        checker = ROOT / "scripts" / "check_dependency_lock.py"
        self.assertTrue(checker.is_file())
        self.assertNotIn("--check", checker.read_text(encoding="utf-8"))

    def test_setup_and_workflows_use_the_shared_lock(self):
        setup = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
        deploy = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
        self.assertIn('"requirements-lock.txt"', setup)
        self.assertIn('"requirements-lock.txt"', deploy)
        self.assertIn('"-c", $lockFile', setup)
        self.assertIn('"-c", $lockFile', deploy)

        for workflow_name in ("ci.yml", "release.yml"):
            workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(
                encoding="utf-8"
            )
            self.assertIn("requirements-lock.txt", workflow)
            self.assertIn("-c requirements-lock.txt", workflow)
            self.assertIn("scripts/check_dependency_lock.py", workflow)
            self.assertNotIn("pip-compile --check", workflow)
            self.assertRegex(workflow, r"uses: actions/[^@]+@[0-9a-f]{40}")

    def test_release_has_sbom_and_provenance_contract(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("ClarifyVoice.sbom.json", workflow)
        self.assertIn("actions/attest-build-provenance@", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("attestations: write", workflow)


if __name__ == "__main__":
    unittest.main()
