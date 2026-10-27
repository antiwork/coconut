"""config_paths points the values of a moved profile's config.yaml at its new home. test_config_yaml_style.py runs the
same edit through the config reader and writer of Hermes, with block lists, inline lists and quoted values."""
import unittest

from coconut import config_paths

OLD, NEW = '/data/bots/41ba/home', '/Users/me/.hermes/profiles/ada'


class Quoted(str):
    """Stands in for a ruamel scalar string, whose type keeps the quote style."""


class ConfigPathTests(unittest.TestCase):
    def test_each_string_value_in_the_old_home_moves_at_any_depth(self):
        config = {
            'terminal': {'shell_init_files': [f'{OLD}/.profile', '/etc/zshrc', f'{OLD}/.zshrc'], 'cwd': OLD},
            'hooks': [{'path': f'{OLD}/hooks/a.sh', 'deep': {'list': [[f'{OLD}/x']]}}],
            'model': {'default': 'gpt', 'max_tokens': 3, 'stream': True, 'base_url': None},
        }
        changed = config_paths.with_paths(config, OLD, NEW)
        self.assertEqual(changed, {
            'terminal': {'shell_init_files': [f'{NEW}/.profile', '/etc/zshrc', f'{NEW}/.zshrc'], 'cwd': NEW},
            'hooks': [{'path': f'{NEW}/hooks/a.sh', 'deep': {'list': [[f'{NEW}/x']]}}],
            'model': {'default': 'gpt', 'max_tokens': 3, 'stream': True, 'base_url': None},
        })
        # The input stays as it was.
        self.assertEqual(config['terminal']['cwd'], OLD)

    def test_a_lookalike_path_and_a_key_stay(self):
        config = {'a': f'{OLD}-sibling/x', 'b': f'/mirror{OLD}/x', 'c': f'/a:{OLD}/bin', 'd': f'{OLD}x', OLD: 'key'}
        self.assertIsNone(config_paths.with_paths(config, OLD, NEW))

    def test_a_quoted_value_keeps_its_type(self):
        changed = config_paths.with_paths({'files': [Quoted(f'{OLD}/.profile')]}, OLD, NEW)
        self.assertEqual(changed['files'], [f'{NEW}/.profile'])
        self.assertIs(type(changed['files'][0]), Quoted)

    def test_the_command_needs_pairs(self):
        self.assertEqual(config_paths.main(['/hermes/source', OLD]), 64)


if __name__ == '__main__':
    unittest.main()
