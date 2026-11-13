"""The `coconut` skill meets the Hermes authoring rules, ships in the source package, and setup adds its folder to the
Hermes profiles without other changes. test_config_yaml_style.py checks the same edit through the config writer of Hermes."""
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from coconut import skill_dirs

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / 'skills/coconut'
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import remote  # noqa: E402

spec = importlib.util.spec_from_file_location('coconut_skill_deploy', SCRIPTS / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)


def frontmatter(text):
    """The top-level fields of the frontmatter, and the text after it. The fields are simple `key: value` lines."""
    match = re.match(r'---\n(.*?)\n---\n', text, re.S)
    fields = dict(re.findall(r'^([a-z_]+):[ ]?(.*)$', match[1], re.M))
    return fields, match[1], text[match.end():]


class SkillAuthoringTests(unittest.TestCase):
    def setUp(self):
        self.text = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
        self.fields, self.header, self.body = frontmatter(self.text)

    def test_the_frontmatter_has_the_fields_that_hermes_reads(self):
        self.assertEqual(self.fields['name'], SKILL.name)
        self.assertRegex(self.fields['version'], r'^\d+\.\d+\.\d+$')
        self.assertTrue(self.fields['author'])
        self.assertEqual(self.fields['platforms'], '[macos]')
        self.assertRegex(self.header, r'\nmetadata:\n  hermes:\n    tags: \[[a-z0-9, -]+\]\n')

    def test_the_description_is_one_short_sentence(self):
        description = self.fields['description']
        self.assertLessEqual(len(description), 60)
        self.assertTrue(description.endswith('.'))
        self.assertEqual(description.count('. '), 0)
        self.assertNotIn(':', description)  # A colon would need quotes in YAML.

    def test_the_hub_names_each_reference_and_each_reference_exists(self):
        named = set(re.findall(r'`(references/[a-z-]+\.md)`', self.body))
        present = {path.relative_to(SKILL).as_posix() for path in (SKILL / 'references').glob('*.md')}
        self.assertEqual(named, present)
        for path in SKILL.rglob('*.md'):
            for name in re.findall(r'`(references/[a-z-]+\.md)`', path.read_text(encoding='utf-8')):
                with self.subTest(file=path.name, reference=name):
                    self.assertTrue((SKILL / name).is_file())

    def test_the_routines_reference_gives_the_gate_and_the_wait_for_delegated_work(self):
        text = (SKILL / 'references/routines.md').read_text(encoding='utf-8')
        self.assertIn('{"wakeAgent": false}', text)
        self.assertIn('--no-agent', text)
        self.assertIn('hermes cron edit <id> --script <file>', text)
        self.assertIn('Wake the agent when in doubt', text)
        self.assertIn('Do not create a cron job to poll', text)
        self.assertIn('`coconut_wait`', text)
        # The hub routes routine questions to it.
        self.assertIn('`references/routines.md`', self.body.split('## When to Use')[1])

    def test_the_pages_reference_gives_the_recipe_the_rules_and_an_example_without_notes(self):
        from coconut.documents import LONGEST_HEADER, page_notes
        text = (SKILL / 'references/pages.md').read_text(encoding='utf-8')
        self.assertLessEqual(len(text.splitlines()), 120)
        self.assertTrue(text.startswith('# '))
        for heading in ('## The recipe of a report page', '## The blocks', '## What the phone and the Mac do with a table',
                        '## The rules for content', '## The notes of a save', '## Example: the weekly report of a shop'):
            with self.subTest(heading=heading):
                self.assertIn('\n' + heading + '\n', text)
        self.assertIn(f'A column header has at most {LONGEST_HEADER} characters.', text)
        self.assertIn('Follow the notes in the next save of the same key.', text)
        # The example is one fenced block. test_page_notes.py and the app tests (LibraryDocumentTests) read it too.
        example = text.split('````markdown\n', 1)[1].split('\n````', 1)[0]
        self.assertEqual(page_notes('markdown', example), [])
        # The hub routes page questions to it, and the save tool names it.
        self.assertIn('`references/pages.md`', self.body.split('## When to Use')[1])
        from coconut.documents import BLOCKS
        self.assertIn('references/pages.md', BLOCKS)

    def test_the_skill_names_hermes_tools_and_no_wrapped_shell_utilities(self):
        for path in SKILL.rglob('*.md'):
            text = path.read_text(encoding='utf-8')
            with self.subTest(file=path.name):
                self.assertNotRegex(text, r'`(?:grep|cat|head|tail|sed|awk|find|ls)\b')
        self.assertIn('`terminal`', self.body)
        self.assertIn('`skill_view`', self.body)

    def test_the_skill_names_only_real_coconut_commands(self):
        usage = (ROOT / 'mac/Sources/CoconutTool/main.swift').read_text(encoding='utf-8')
        commands = set(re.findall(r'^      ([a-z-]+)', usage, re.M))
        for path in SKILL.rglob('*.md'):
            for command in re.findall(r'`coconut ([a-z-]+)', path.read_text(encoding='utf-8')):
                with self.subTest(file=path.name, command=command):
                    self.assertIn(command, commands | {'help'})

    def test_the_skill_ships_in_the_source_package(self):
        files = remote.source_files()
        expected = {path.relative_to(ROOT).as_posix() for path in SKILL.rglob('*.md')}
        self.assertIn('skills/coconut/SKILL.md', expected)
        self.assertLessEqual(expected, set(files))
        for name in expected:
            with self.subTest(name=name):
                self.assertTrue(deploy.allowed_path(name))
        for name in ('skills/SKILL.md', 'skills/coconut/run.sh', 'skills/coconut/../x.md', 'skills//coconut/SKILL.md'):
            with self.subTest(name=name):
                self.assertFalse(deploy.allowed_path(name))

    def test_the_gateway_platform_ships_in_the_source_package(self):
        files = remote.source_files()
        expected = {path.relative_to(ROOT).as_posix() for path in (ROOT / 'hermes_plugin/coconut').iterdir()
                    if path.suffix in ('.py', '.yaml')}
        self.assertEqual(expected, {'hermes_plugin/coconut/plugin.yaml', 'hermes_plugin/coconut/__init__.py',
                                    'hermes_plugin/coconut/adapter.py', 'hermes_plugin/coconut/cron.py', 'hermes_plugin/coconut/hooks.py',
                                    'hermes_plugin/coconut/team.py', 'hermes_plugin/coconut/tools.py'})
        self.assertLessEqual(expected, set(files))
        for name in expected:
            with self.subTest(name=name):
                self.assertTrue(deploy.allowed_path(name))
        for name in ('hermes_plugin/other/adapter.py', 'hermes_plugin/coconut/run.sh', 'hermes_plugin/coconut/x/y.py'):
            with self.subTest(name=name):
                self.assertFalse(deploy.allowed_path(name))


FOLDER = '/Users/person/Coconut/skills'


class SkillFolderEditTests(unittest.TestCase):
    def test_add_creates_a_missing_skills_section_and_keeps_the_other_keys(self):
        config = {'model': {'default': 'gpt'}, 'toolsets': ['kanban']}
        changed = skill_dirs.with_folder(config, FOLDER, True)
        self.assertEqual(changed, {'model': {'default': 'gpt'}, 'toolsets': ['kanban'],
                                   'skills': {'external_dirs': [FOLDER]}})
        self.assertEqual(config, {'model': {'default': 'gpt'}, 'toolsets': ['kanban']})

    def test_add_appends_to_the_folders_and_settings_of_the_person(self):
        config = {'skills': {'external_dirs': ['~/.agents/skills'], 'disabled': ['x']}}
        changed = skill_dirs.with_folder(config, FOLDER, True)
        self.assertEqual(changed['skills'], {'external_dirs': ['~/.agents/skills', FOLDER], 'disabled': ['x']})

    def test_add_is_idempotent(self):
        once = skill_dirs.with_folder({}, FOLDER, True)
        self.assertIsNone(skill_dirs.with_folder(once, FOLDER, True))
        self.assertIsNone(skill_dirs.with_folder({'skills': None, 'x': 1} | once, FOLDER, True))

    def test_an_entry_in_another_spelling_counts_as_present(self):
        home = '/Users/person/.hermes'
        with patch.dict('os.environ', {'HOME': '/Users/person', 'COCONUT': '/Users/person/Coconut'}):
            for entry in ('~/Coconut/skills', '${COCONUT}/skills', '/Users/person/Coconut/skills/', '../Coconut/skills'):
                with self.subTest(entry=entry):
                    self.assertIsNone(skill_dirs.with_folder({'skills': {'external_dirs': [entry]}}, FOLDER, True, home))

    def test_a_single_string_becomes_a_list(self):
        changed = skill_dirs.with_folder({'skills': {'external_dirs': '/shared/skills'}}, FOLDER, True)
        self.assertEqual(changed['skills']['external_dirs'], ['/shared/skills', FOLDER])

    def test_a_form_that_hermes_does_not_read_stays(self):
        for config in ({'skills': 'off'}, {'skills': {'external_dirs': {'a': 1}}}, {'skills': ['x']}):
            with self.subTest(config=config):
                self.assertIsNone(skill_dirs.with_folder(config, FOLDER, True))
                self.assertIsNone(skill_dirs.with_folder(config, FOLDER, False))

    def test_remove_takes_out_only_the_coconut_folder(self):
        config = {'skills': {'external_dirs': ['/shared/skills', FOLDER], 'disabled': ['x']}, 'model': 'gpt'}
        self.assertEqual(skill_dirs.with_folder(config, FOLDER, False),
                         {'skills': {'external_dirs': ['/shared/skills'], 'disabled': ['x']}, 'model': 'gpt'})

    def test_remove_deletes_the_sections_that_it_empties(self):
        added = skill_dirs.with_folder({'model': 'gpt'}, FOLDER, True)
        self.assertEqual(skill_dirs.with_folder(added, FOLDER, False), {'model': 'gpt'})
        kept = {'skills': {'external_dirs': [FOLDER], 'disabled': ['x']}}
        self.assertEqual(skill_dirs.with_folder(kept, FOLDER, False), {'skills': {'disabled': ['x']}})
        self.assertIsNone(skill_dirs.with_folder({'model': 'gpt'}, FOLDER, False))

    def test_the_homes_are_the_default_profile_its_named_profiles_and_the_bots(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / 'hermes'
            home.mkdir()
            bot = Path(temporary) / 'bots/one/home'
            # Without a profiles folder, only the default home and the bots change.
            self.assertEqual(skill_dirs.homes(home, [bot]), [(home, True), (bot, False)])
            for name in ('work', 'chat'):
                (home / 'profiles' / name).mkdir(parents=True)
            (home / 'profiles/notes.txt').write_text('A file is not a profile.')
            self.assertEqual(skill_dirs.homes(home), [(home, True), (home / 'profiles/chat', False),
                                                      (home / 'profiles/work', False)])

    def test_a_home_without_a_config_changes_only_when_it_can_be_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            # A named profile or a bot without config.yaml stays as it is. No Hermes module loads for it.
            self.assertEqual(skill_dirs.update(home, FOLDER, True), 'skipped')
            self.assertEqual(skill_dirs.update(home, FOLDER, False, create=True), 'skipped')
            self.assertEqual(skill_dirs.update(home / 'absent', FOLDER, True, create=True), 'skipped')


if __name__ == '__main__':
    unittest.main()
