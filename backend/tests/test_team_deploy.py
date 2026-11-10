"""The deploy package carries the sources of the team helper, and a deploy builds the helper (scripts/team.py)."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import remote  # noqa: E402
import team  # noqa: E402

spec = importlib.util.spec_from_file_location('coconut_team_deploy', SCRIPTS / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)


class TeamHelperPackageTests(unittest.TestCase):
    def test_every_helper_source_and_the_script_are_in_the_deploy_package(self):
        files = remote.source_files()
        for source in (*team.SOURCES, SCRIPTS / 'team.py'):
            name = source.relative_to(remote.ROOT).as_posix()
            with self.subTest(name=name):
                self.assertIn(name, files)
                self.assertTrue(deploy.allowed_path(name))

    def test_the_prepare_step_builds_the_push_helper_then_the_team_helper(self):
        root = Path('/Coconut')
        commands = []

        def command(self, arguments, root, phase, **options):
            commands.append((arguments[1:], phase))
        with patch.object(deploy.Operations, 'command', command):
            deploy.Operations.prepare(object.__new__(deploy.Operations), root, None)
        self.assertEqual(commands, [([str(root / 'scripts/push.py'), '--rebuild'], 'Notification encryption build'),
                                    ([str(root / 'scripts/team.py'), 'build'], 'Team helper build')])

    def test_a_failed_team_helper_build_stops_the_prepare_step(self):
        def command(self, arguments, root, phase, **options):
            if phase == 'Team helper build':
                raise deploy.DeployError('Team helper build failed (exit 1). No command output was exposed.')
        with patch.object(deploy.Operations, 'command', command), self.assertRaises(deploy.DeployError):
            deploy.Operations.prepare(object.__new__(deploy.Operations), Path('/Coconut'), None)


class TeamHelperBuildTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)

    def test_under_the_app_the_build_compiles_nothing(self):
        with patch.dict(os.environ, {team.HELPER_ENVIRONMENT: '/Applications/Coconut.app/Contents/Helpers/team-helper'}), \
                patch.object(team, 'compile_swift') as compile_swift:
            self.assertEqual(team.build(self.data), Path('/Applications/Coconut.app/Contents/Helpers/team-helper'))
        compile_swift.assert_not_called()
        self.assertEqual((self.data / 'team').stat().st_mode & 0o777, 0o700)

    def test_a_source_install_compiles_into_the_private_team_folder(self):
        with patch.dict(os.environ, {team.HELPER_ENVIRONMENT: ''}), patch.object(team, 'compile_swift') as compile_swift:
            self.assertEqual(team.build(self.data), self.data.resolve() / 'team/team-helper')
        compile_swift.assert_called_once_with(team.SOURCES, self.data.resolve() / 'team/team-helper',
                                              self.data.resolve() / 'team/helper.sha256')

    def test_the_build_refuses_a_team_folder_that_is_a_link(self):
        (self.data / 'elsewhere').mkdir()
        (self.data / 'team').symlink_to(self.data / 'elsewhere')
        with patch.object(team, 'compile_swift') as compile_swift, self.assertRaises(SystemExit):
            team.build(self.data)
        compile_swift.assert_not_called()


if __name__ == '__main__':
    unittest.main()
