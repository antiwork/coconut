"""Guard: the text that a person or a model reads says "agent", not "bot".

The test reads only backend/, hermes_plugin/, scripts/, skills/, README.md, CONTRIBUTING.md, docs/ and the pairing
message in ios/Coconut/Resources/. scripts/ci_changes.py lets a pull request that changes only ios/ or mac/ skip this
test, so the test must not read those folders. ios/Coconut/Resources/ is the exception: NEEDED in scripts/ci_changes.py
lists it. mac/Tests/CoconutKitTests/AgentWordTests.swift checks the text of the apps.
"""
import ast
import io
from pathlib import Path
import re
import tokenize
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

# A word boundary is a character that is not a letter, a digit, '_', '-' or '/'. So 'bot_id', '/v1/bots' and 'new-bot'
# are names, and 'the bot' is a word. A dot between two word characters also joins a name, such as the event type
# 'bot.read' or the column 'm.bot', but a dot after the word ends a sentence.
WORD = re.compile(r'(?<![\w/-])(?<!\w\.)[Bb]ots?(?![\w/-])(?!\.\w)')
# A name in code or data, such as 'bots', 'bot_id', '/v1/bots' or 'bot.created'. Each other literal can be text.
NAME = re.compile(r'[a-z0-9_.:/-]+')
# The earlier names of the `coconut team` commands. They still work, but a person must read only the new names.
OLD_COMMANDS = ('coconut team new-bot', 'coconut team add-bot', 'coconut team remove-bot')
# Outside meanings of the word, allowed in each file and in each kind of text. A Telegram bot is a Telegram account
# that a program runs. A bot account is a GitHub account of an app. AgentWordTests.swift allows the same phrases.
PHRASES = re.compile(r'(?<![\w/-])(?:Telegram bots?|bot accounts?)(?![\w/-])')
# Files that the test reads by name. Each must exist, so that a move of such a file also changes the test.
NAMED_FILES = ('README.md', 'CONTRIBUTING.md', 'hermes_plugin/coconut/plugin.yaml',
               'ios/Coconut/Resources/hermes-pairing-message.txt')

# Calls that write a log line. A log line is for the developer, so it can keep the old word.
LOG_OBJECTS = {'LOG', 'log', 'logger', 'logging'}
LOG_METHODS = {'debug', 'info', 'warning', 'warn', 'error', 'exception', 'critical', 'log'}
LOG_FUNCTIONS = {'log'}
# SQL text, also a part of a query that a constant keeps. SQL keywords are in capital letters in this repository. The
# literal must have the shape of a statement or a clause. So a sentence with a capital SQL word, such as "Run UPDATE
# first, then ask the bot.", is still text.
SQL = re.compile(r'\bSELECT\b[\s\S]*?\bFROM\b|\bUPDATE\s+\w+\s+SET\b|\bINSERT\s+(?:OR\s+\w+\s+)?INTO\b'
                 r'|\bDELETE\s+FROM\b|\b(?:CREATE(?:\s+UNIQUE)?|DROP)\s+(?:TABLE|INDEX)\b'
                 r'|\bWHERE\s+[\w.]+\s*(?:[=<>!]|IS\b|IN\b|LIKE\b)|[\w.]+\s+IS\s+(?:NOT\s+)?NULL\b'
                 r'|\b(?:ORDER|GROUP)\s+BY\b|\bjson_(?:extract|type)\(')

# Each allowed text: (file, text, reason). A text of one word allows only a literal that is exactly that word. A longer
# text is removed from the literal before the test looks for the word, so the rest of the literal is still checked.
ALLOWED = (
    ('backend/coconut/service.py', 'Bot messages paused after eight deliveries.',
     'The service matches this text in messages that older versions stored.'),
    ('backend/coconut/routine_coordinator.py', 'serves the profile of this bot now',
     'The pause of a routine matches this reason in routines that older versions stored.'),
    ('backend/coconut/routine_coordinator.py', 'The Hermes profile of this bot is parked (',
     'The pause of a routine matches this reason in routines that older versions stored.'),
    ('backend/coconut/pr_tracking.py', 'Bot',
     'GitHub sets the user type "Bot" for a GitHub App account.'),
)


def python_files():
    files = sorted((ROOT / 'backend' / 'coconut').rglob('*.py')) + sorted((ROOT / 'hermes_plugin').rglob('*.py'))
    files += sorted(path for path in (ROOT / 'scripts').glob('*.py') if not path.name.startswith('test_'))
    return files


def markdown_files():
    files = [ROOT / name for name in NAMED_FILES if name.endswith('.md')]
    files += sorted((ROOT / 'docs').rglob('*.md')) + sorted((ROOT / 'skills').rglob('*.md'))
    return [path for path in files if path.is_file()]


def model_text_files():
    """Return the other named files: a model reads the pairing message and the manifest of the Hermes plugin."""
    return [ROOT / name for name in NAMED_FILES if not name.endswith('.md') and (ROOT / name).is_file()]


def name_of(node):
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ''


def is_log_call(node):
    function = node.func
    if isinstance(function, ast.Name):
        return function.id in LOG_FUNCTIONS
    if not isinstance(function, ast.Attribute) or function.attr not in LOG_METHODS:
        return False
    owner = function.value
    if isinstance(owner, ast.Call):  # logging.getLogger(__name__).warning(...)
        return name_of(owner.func) == 'getLogger'
    return name_of(owner) in LOG_OBJECTS


def skipped_nodes(tree):
    """Return the ids of the nodes whose strings the test does not check."""
    skipped = set()

    def skip(node):
        skipped.update(id(child) for child in ast.walk(node))

    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Constant, ast.JoinedStr)):
            skip(node)  # A docstring or another string statement.
        elif isinstance(node, ast.Call) and is_log_call(node):
            for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
                skip(argument)
        elif isinstance(node, ast.JoinedStr):
            # The test checks the text of an f-string as one literal, so it skips the constant parts of that text. It
            # checks the strings in the expressions of the f-string as literals of their own.
            skipped.update(id(part) for part in node.values if isinstance(part, ast.Constant))
    return skipped


def literal_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(part.value if isinstance(part, ast.Constant) else '{}' for part in node.values)
    return None


def says_old_word(text):
    """Return True when the text has the old word outside an allowed phrase."""
    return bool(WORD.search(PHRASES.sub(' ', text)))


def fails(text, allowed=()):
    """Return True when a literal names an earlier command, or says the old word and is not a name or SQL text."""
    if any(command in text for command in OLD_COMMANDS):
        return True
    if NAME.fullmatch(text) or SQL.search(text):
        return False
    for exception in allowed:
        if text == exception:
            return False
        if ' ' in exception:
            text = text.replace(exception, ' ')
    return says_old_word(text)


def printed_docstring_lines(tree, allowed):
    """Yield the line number and the text of each failing line of the docstring of a module that reads `__doc__`.

    Such a module prints its docstring as help or usage, for example with `description=__doc__` or `print(__doc__)`.
    The test checks each line of that docstring as a literal of its own. It skips each other docstring.
    """
    first = tree.body[0] if tree.body else None
    if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        return
    if any(isinstance(node, ast.Name) and node.id == '__doc__' for node in ast.walk(tree)):
        for index, line in enumerate(first.value.value.splitlines()):
            if fails(line, allowed):
                yield first.lineno + index, line.strip()


def argument_names(tree):
    """Yield the line and the text of each argparse name that argparse prints and that says the old word.

    argparse prints the name of a positional argument in `usage:` and in its errors, unless the call gives a `metavar`.
    It always prints the metavar. So `add_argument('bot')` fails, although 'bot' is a name.
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and name_of(node.func) == 'add_argument'):
            continue
        shown = next((keyword.value for keyword in node.keywords if keyword.arg == 'metavar'), None)
        first = node.args[0] if node.args else None
        if shown is None and isinstance(first, ast.Constant) and isinstance(first.value, str):
            shown = None if first.value.startswith('-') else first
        text = literal_text(shown) if shown is not None else None
        if text is not None and says_old_word(text):
            yield shown.lineno, text


def string_lines(source):
    """Return the start line and the text of each string token, so that a long literal reports its exact line."""
    tokens = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.STRING or token.type == getattr(tokenize, 'FSTRING_MIDDLE', -1):
            tokens.append((token.start[0], token.end[0], token.string))
    return tokens


def word_line(node, tokens):
    for start, end, text in tokens:
        if node.lineno <= start and end <= (node.end_lineno or node.lineno) and WORD.search(text):
            return start + next((index for index, line in enumerate(text.splitlines()) if WORD.search(line)), 0)
    return node.lineno


def python_findings(path):
    source = path.read_text()
    relative = path.relative_to(ROOT).as_posix()
    allowed = [text for file, text, _ in ALLOWED if file == relative]
    tree = ast.parse(source, filename=relative)
    skipped = skipped_nodes(tree)
    tokens = None
    findings = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        text = literal_text(node)
        if text is None or not fails(text, allowed):
            continue
        tokens = tokens if tokens is not None else string_lines(source)
        findings.append((word_line(node, tokens), text))
    findings += printed_docstring_lines(tree, allowed)
    findings += argument_names(tree)
    return [f'{relative}:{line}: {text!r}' for line, text in sorted(set(findings))]


def prose_lines(text):
    """Yield each line number and its prose: the line without fenced code blocks, code spans and link targets."""
    fence = None
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith(('```', '~~~')):
            fence = stripped[:3]
            continue
        line = re.sub(r'(`+).*?\1', ' ', line)
        line = re.sub(r'\]\([^)]*\)', ']', line)
        yield number, line


def markdown_findings(path):
    """Find each line that names an earlier command, also in code, and each line whose prose says the old word."""
    relative = path.relative_to(ROOT).as_posix()
    text = path.read_text()
    prose = dict(prose_lines(text))
    return [f'{relative}:{number}: {line.strip()!r}' for number, line in enumerate(text.splitlines(), start=1)
            if any(command in line for command in OLD_COMMANDS) or says_old_word(prose.get(number, ''))]


def model_text_findings(path):
    """Find each line of a text for a model that names an earlier command or says the old word.

    A model reads the whole pairing message. Of a YAML file, the test reads the text values: the text after a key at
    the start of a line, and each indented line, such as a line of a folded value. A comment is not a value.
    """
    relative = path.relative_to(ROOT).as_posix()
    findings = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if path.suffix == '.yaml':
            if line.lstrip().startswith('#'):
                continue
            line = re.sub(r'^[\w.-]+:', ' ', line)
        if any(command in line for command in OLD_COMMANDS) or says_old_word(line):
            findings.append(f'{relative}:{number}: {line.strip()!r}')
    return findings


def message(findings):
    return ('These texts say "bot". Write "agent" ("bots" -> "agents", "a bot" -> "an agent"), write the commands '
            '"coconut team new-agent", "add-agent" and "remove-agent", and give an argparse argument "bot" the '
            'metavar "agent":\n' + '\n'.join(findings))


def findings_of(source, path='backend/coconut/example.py'):
    """Return the findings of a file of the repository that has this text."""
    read = {'.py': python_findings, '.md': markdown_findings}.get(Path(path).suffix, model_text_findings)
    with patch.object(Path, 'read_text', return_value=source):
        return read(ROOT / path)


class VisibleWordTests(unittest.TestCase):
    def test_python_strings_say_agent(self):
        findings = [finding for path in python_files() for finding in python_findings(path)]
        if findings:
            self.fail(message(findings))

    def test_markdown_prose_says_agent(self):
        findings = [finding for path in markdown_files() for finding in markdown_findings(path)]
        if findings:
            self.fail(message(findings))

    def test_the_pairing_message_and_the_plugin_manifest_say_agent(self):
        findings = [finding for path in model_text_files() for finding in model_text_findings(path)]
        if findings:
            self.fail(message(findings))

    def test_each_named_file_exists(self):
        for name in NAMED_FILES:
            with self.subTest(file=name):
                self.assertTrue((ROOT / name).is_file(), f'The guard reads {name}, but this file does not exist. '
                                                         'When you move or remove the file, change NAMED_FILES.')

    def test_each_allowed_text_is_still_in_its_file(self):
        for file, text, reason in ALLOWED:
            with self.subTest(file=file, reason=reason):
                self.assertIn(text, (ROOT / file).read_text())

    def test_the_rule_finds_the_word_only_in_text(self):
        self.assertTrue(fails('Restore the bot before answering.'))
        self.assertTrue(fails("The bot's reply"))
        self.assertTrue(fails('Restore the bot.'))
        self.assertFalse(fails('Bot', allowed=['Bot']))
        self.assertTrue(fails('The Bot type', allowed=['Bot']))
        old = 'serves the profile of this bot now'
        self.assertFalse(fails(f'The routine waits: Ada {old}.', allowed=[old]))
        self.assertTrue(fails(f'The bot waits: Ada {old}.', allowed=[old]))
        for text in ('Bot', 'Bots', 'Bot:', 'Bots,', '(bot)', 'bot\n', 'run coconut team add-bot Ada',
                     'coconut team new-bot', 'coconut team remove-bot Ada'):
            with self.subTest(text=text):
                self.assertTrue(fails(text))
        for name in ('bot_id', '/v1/bots', 'bots', 'bot', 'new-bot', 'bot.created', 'create-bot', 'bots:', 'the robot',
                     'both bottom', 'bot.read bots.addressed', 'm.bot = ?', 'SELECT id FROM bots',
                     'INSERT OR IGNORE INTO team_shares (room, bot, entry) VALUES (?, ?, ?)'):
            with self.subTest(name=name):
                self.assertFalse(fails(name))

    def test_the_sql_rule_needs_a_statement_or_a_clause(self):
        for text in ('Run UPDATE first, then ask the bot.', 'The bot EXISTS.', 'Give the VALUES to the bot.',
                     'Ask the bot WHERE it runs.', 'SELECT a bot.', 'The bot IS NOT here.'):
            with self.subTest(text=text):
                self.assertTrue(fails(text))
        for text in ('SELECT data FROM bots WHERE id=?', 'UPDATE bots SET data=? WHERE id=?', 'DELETE FROM bots',
                     'CREATE TABLE IF NOT EXISTS team_shares (room TEXT NOT NULL, bot TEXT NOT NULL)',
                     'CREATE UNIQUE INDEX shares ON team_shares (bot)', 'DROP INDEX IF EXISTS team_shares_bot (bot)',
                     'WHERE bot = ?', ' AND (bot IS NULL OR bot IN (?))', ' ORDER BY bot', 'GROUP BY room, bot',
                     "json_extract(data, '$.bot')=?", "json_type(data, '$.bot')",
                     'INSERT OR IGNORE INTO team_triggers (room, bot, entry) VALUES (?, ?, ?)'):
            with self.subTest(text=text):
                self.assertFalse(fails(text))

    def test_the_rule_allows_the_outside_meanings_of_the_word(self):
        for text in ('Message the Telegram bot of your Hermes.', 'Telegram bots do not encrypt a chat end to end.',
                     'The author is a bot account.', 'GitHub makes bot accounts for apps.'):
            with self.subTest(text=text):
                self.assertFalse(fails(text))
        for text in ('The bot is a bot account.', 'Ask the bot, not the Telegram bot.', 'Telegram botany for a bot'):
            with self.subTest(text=text):
                self.assertTrue(fails(text))
        text = 'Telegram does not encrypt a chat with a Telegram bot end to end.\nTelegram does not encrypt bot chats.'
        self.assertEqual([finding.split(':')[1] for finding in findings_of(text, 'docs/example.md')], ['2'])

    def test_the_rule_skips_docstrings_comments_logs_and_sql(self):
        source = '\n'.join((
            '"""The bot module."""',
            'def f(name):',
            '    """Start the bot."""',
            '    # The bot answers.',
            "    LOG.info('the bot started %s', name)",
            "    logging.getLogger(__name__).warning('the bot failed %s', name)",
            "    db.execute('SELECT id FROM bots WHERE name = ?', (name,))",
            "    query = 'SELECT id FROM bots'",
            "    return f'{name}: {query}'",
        ))
        self.assertEqual(findings_of(source), [])

    def test_the_rule_reads_the_docstring_of_a_module_that_prints_it(self):
        source = '\n'.join((
            '"""Connect the team chat of a bot of a team-only Mac to its Hermes home.',
            '',
            '    python3 team_chat.py connect <bot>',
            'The service reads SELECT data FROM bots.',
            '"""',
            'import sys',
            '',
            '',
            'def main(arguments):',
            '    """Start the bot."""',
            '    print(__doc__, file=sys.stderr)',
        ))
        self.assertEqual(findings_of(source), [
            "backend/coconut/example.py:1: 'Connect the team chat of a bot of a team-only Mac to its Hermes home.'",
            "backend/coconut/example.py:3: 'python3 team_chat.py connect <bot>'"])
        argparse_source = source.replace("print(__doc__, file=sys.stderr)", "ArgumentParser(description=__doc__)")
        self.assertEqual(len(findings_of(argparse_source)), 2)
        self.assertEqual(findings_of(source.replace("print(__doc__, file=sys.stderr)", "print('usage')")), [])

    def test_the_rule_reads_the_names_that_argparse_prints(self):
        source = '\n'.join((
            "parser.add_argument('bot')",
            "parser.add_argument('bots', nargs='*')",
            "parser.add_argument('bot', metavar='agent')",
            "parser.add_argument('--bot', help='The agent.')",
            "parser.add_argument('bot_id')",
            "command.add_argument('agent', metavar='bot')",
        ))
        self.assertEqual(findings_of(source), ["backend/coconut/example.py:1: 'bot'",
                                               "backend/coconut/example.py:2: 'bots'",
                                               "backend/coconut/example.py:6: 'bot'"])
        self.assertIn('metavar "agent"', message(findings_of(source)))

    def test_the_rule_reads_the_expressions_of_an_f_string_and_the_values_of_a_query(self):
        source = '\n'.join((
            "def f(name, db):",
            "    db.execute('UPDATE rooms SET note = ?', ('Ask the bot.',))",
            "    return f\"{name or 'the bot'}\"",
        ))
        self.assertEqual(findings_of(source), ["backend/coconut/example.py:2: 'Ask the bot.'",
                                               "backend/coconut/example.py:3: 'the bot'"])

    def test_a_finding_names_the_file_the_line_and_the_text(self):
        source = '\n'.join((
            "MESSAGE = ('The service runs. '",
            "           'Restore the bot first.')",
            "NAME = f'Name one other bot exactly: {name}.'",
        ))
        findings = findings_of(source)
        self.assertEqual(findings, ["backend/coconut/example.py:2: 'The service runs. Restore the bot first.'",
                                    "backend/coconut/example.py:3: 'Name one other bot exactly: {}.'"])
        self.assertIn('Write "agent"', message(findings))
        self.assertIn('"coconut team new-agent"', message(findings))

    def test_the_rule_reads_markdown_prose_and_the_earlier_commands_also_in_code(self):
        text = '\n'.join(('Each bot has a profile.', '```', 'ls bots/', '```', 'Run `bots/Ada`.',
                          'See [the agents](docs/bots.md).', 'Run `coconut team add-bot Ada`.', '```',
                          'coconut team new-bot Ada', '```', 'Run `coconut team add-agent Ada`.'))
        self.assertEqual([finding.split(':')[1] for finding in findings_of(text, 'docs/example.md')], ['1', '7', '9'])

    def test_the_rule_reads_the_pairing_message_and_the_values_of_the_plugin_manifest(self):
        text = 'Hi Hermes.\nAsk the bot to run it.\nThen run coconut team new-bot Ada.\nbots/ stays.'
        findings = findings_of(text, 'ios/Coconut/Resources/hermes-pairing-message.txt')
        self.assertEqual([finding.split(':')[1] for finding in findings], ['2', '3'])
        manifest = '# The bot plugin.\nname: coconut\nbot: x\ndescription: >\n  Chat turns from the bot.\nlabel: Bot\n'
        findings = findings_of(manifest, 'hermes_plugin/coconut/plugin.yaml')
        self.assertEqual([finding.split(':')[1] for finding in findings], ['5', '6'])


if __name__ == '__main__':
    unittest.main()
