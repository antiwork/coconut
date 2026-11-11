"""Tests of scripts/ci_changes.py, the rule that skips the service checks of a pull request."""
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_changes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class SkippableGroupTests(unittest.TestCase):
    def test_a_change_of_the_iphone_app_skips(self):
        self.assertEqual(ci_changes.decide(['ios/Coconut/ChatView.swift', 'ios/project.yml', 'ios/Packages/Kit/Package.swift',
                                            'ios/CoconutTests/ChatTests.swift', 'ios/Coconut.xcodeproj/project.pbxproj']),
                         'skip')

    def test_a_change_of_the_mac_app_skips(self):
        self.assertEqual(ci_changes.decide(['mac/Sources/CoconutApp/App.swift', 'mac/VERSION', 'mac/Package.swift']), 'skip')

    def test_a_change_of_the_relay_skips(self):
        self.assertEqual(ci_changes.decide(['relay/src/room.js', 'relay/test/room.test.js', 'relay/package.json']), 'skip')

    def test_a_change_of_the_update_feed_skips(self):
        self.assertEqual(ci_changes.decide(['updates/install.sh', 'updates/_headers']), 'skip')

    def test_a_change_of_another_workflow_skips(self):
        self.assertEqual(ci_changes.decide(['.github/workflows/ios-tests.yml', '.github/workflows/mac-app.yml']), 'skip')


class NeededFileTests(unittest.TestCase):
    def test_each_service_folder_runs(self):
        for name in ('backend/coconut/service.py', 'backend/tests/test_service.py', 'hermes_plugin/coconut/adapter.py',
                     'scripts/deploy.py', 'skills/coconut/SKILL.md'):
            with self.subTest(name=name):
                self.assertEqual(ci_changes.decide([name]), 'run')

    def test_this_workflow_runs(self):
        self.assertEqual(ci_changes.decide(['.github/workflows/hermes-compatibility.yml']), 'run')

    def test_a_file_that_a_test_reads_runs(self):
        for name in ('ios/Tunnel/Noise.swift', 'ios/Tunnel/NewFrame.swift', 'ios/Coconut/Resources/hermes-pairing-message.txt',
                     'ios/Coconut/Coconut.entitlements',
                     'ios/CoconutNotificationService/CoconutNotificationService.entitlements',
                     'mac/Sources/CoconutTool/main.swift'):
            with self.subTest(name=name):
                self.assertEqual(ci_changes.decide([name]), 'run')

    def test_the_docs_and_the_packed_top_files_run(self):
        # remote.source_files() packs docs/**/*.md and these top files, and test_install_modes.py reads a doc.
        for name in ('docs/architecture.md', 'docs/install-and-update.md', 'README.md', 'Start Coconut.command',
                     'Uninstall Coconut.command'):
            with self.subTest(name=name):
                self.assertEqual(ci_changes.decide([name]), 'run')

    def test_each_needed_path_exists(self):
        # A moved file would leave a stale entry, and the rule would then skip a change that a test reads.
        for name in ci_changes.NEEDED:
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).exists())
        for name in ('ios/Coconut/Coconut.entitlements',
                     'ios/CoconutNotificationService/CoconutNotificationService.entitlements'):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())
                self.assertTrue(ci_changes.needed(name))


class FailSafeTests(unittest.TestCase):
    def test_a_mixed_change_runs(self):
        self.assertEqual(ci_changes.decide(['ios/Coconut/ChatView.swift', 'backend/coconut/service.py']), 'run')
        self.assertEqual(ci_changes.decide(['mac/VERSION', 'ios/Tunnel/Noise.swift']), 'run')

    def test_an_empty_change_runs(self):
        self.assertEqual(ci_changes.decide([]), 'run')
        self.assertEqual(ci_changes.decide(['']), 'run')

    def test_a_rename_out_of_a_service_folder_runs(self):
        # git diff --no-renames writes a rename as the old name and the new name.
        self.assertEqual(ci_changes.decide(['backend/coconut/old.py', 'ios/Coconut/Old.swift']), 'run')

    def test_a_rename_inside_the_app_skips(self):
        self.assertEqual(ci_changes.decide(['ios/Coconut/Old.swift', 'ios/Coconut/New.swift']), 'skip')

    def test_a_deleted_file_counts_as_a_change(self):
        # A deleted file is in the list by its old name, as a changed file is.
        self.assertEqual(ci_changes.decide(['ios/Coconut/Gone.swift']), 'skip')
        self.assertEqual(ci_changes.decide(['ios/Coconut/Gone.swift', 'scripts/gone.py']), 'run')
        self.assertEqual(ci_changes.decide(['ios/Tunnel/Gone.swift']), 'run')

    def test_an_unknown_name_runs(self):
        for name in ('CONTRIBUTING.md', '.gitignore', 'new-folder/file.txt', 'iosx/App.swift', 'ios', 'mac',
                     '.github/CODEOWNERS', '"ios/Coconut/\\303\\251.swift"', '/ios/Coconut/App.swift',
                     'ios/../backend/coconut/service.py', 'ios//Tunnel/Noise.swift'):
            with self.subTest(name=name):
                self.assertEqual(ci_changes.decide([name]), 'run')

    def test_the_command_reads_standard_input_and_writes_one_line(self):
        for text, answer in (('ios/Coconut/App.swift\nmac/VERSION\n', 'skip\n'), ('ios/Coconut/App.swift\nscripts/x.py\n', 'run\n'),
                             ('', 'run\n')):
            with self.subTest(text=text):
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/ci_changes.py')], input=text,
                                        capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout, answer)


if __name__ == '__main__':
    unittest.main()
