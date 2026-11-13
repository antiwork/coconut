"""Guard: the text that a person or a model reads says "agent", not "bot".

The test reads only backend/, hermes_plugin/, scripts/, skills/, README.md and docs/. scripts/ci_changes.py lets a pull
request that changes only ios/ or mac/ skip this test, so the test must not read those folders.
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

# Calls that write a log line. A log line is for the developer, so it can keep the old word.
LOG_OBJECTS = {'LOG', 'log', 'logger', 'logging'}
LOG_METHODS = {'debug', 'info', 'warning', 'warn', 'error', 'exception', 'critical', 'log'}
LOG_FUNCTIONS = {'log'}
# SQL text, also a part of a query that a constant keeps. SQL keywords are in capital letters in this repository.
SQL = re.compile(r'\b(SELECT|INSERT (?:OR \w+ )?INTO|VALUES|UPDATE|DELETE FROM|CREATE TABLE|WHERE|IS NULL|IS NOT NULL|'
                 r'EXISTS)\b')

# Each allowed text: (file, text, reason). A text of one word allows only a literal that is exactly that word. A longer
# text is removed from the literal before the test looks for the word, so the rest of the literal is still checked.
ALLOWED = (
    ('backend/coconut/service.py', 'Bot messages paused after eight deliveries.',
     'The service matches this text in messages that older versions stored.'),
    ('backend/coconut/routine_coordinator.py', 'serves the profile of this bot now',
     'The pause of a routine matches this reason in routines that older versions stored.'),
    ('backend/coconut/routine_coordinator.py', 'The Hermes profile of this bot is parked (',
     'The pause of a routine matches this reason in routines that older versions stored.'),
    ('backend/coconut/native_conversation.py', 'a bot account',
     'A GitHub bot account, not a Coconut agent.'),
    ('backend/coconut/pr_tracking.py', 'Bot',
     'GitHub sets the user type "Bot" for a GitHub App account.'),
)


def python_files():
    files = sorted((ROOT / 'backend' / 'coconut').rglob('*.py')) + sorted((ROOT / 'hermes_plugin').rglob('*.py'))
    files += sorted(path for path in (ROOT / 'scripts').glob('*.py') if not path.name.startswith('test_'))
    return files


def markdown_files():
    files = [ROOT / 'README.md'] + sorted((ROOT / 'docs').rglob('*.md')) + sorted((ROOT / 'skills').rglob('*.md'))
    return [path for path in files if path.is_file()]


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
    return bool(WORD.search(text))


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
    return [f'{relative}:{line}: {text!r}' for line, text in sorted(findings)]


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
            if any(command in line for command in OLD_COMMANDS) or WORD.search(prose.get(number, ''))]


def message(findings):
    return ('These texts say "bot". Write "agent" ("bots" -> "agents", "a bot" -> "an agent"), and write the commands '
            '"coconut team new-agent", "add-agent" and "remove-agent":\n' + '\n'.join(findings))


def findings_of(source, path='backend/coconut/example.py'):
    """Return the findings of a file of the repository that has this text."""
    with patch.object(Path, 'read_text', return_value=source):
        return python_findings(ROOT / path) if path.endswith('.py') else markdown_findings(ROOT / path)


class VisibleWordTests(unittest.TestCase):
    def test_python_strings_say_agent(self):
        findings = [finding for path in python_files() for finding in python_findings(path)]
        if findings:
            self.fail(message(findings))

    def test_markdown_prose_says_agent(self):
        findings = [finding for path in markdown_files() for finding in markdown_findings(path)]
        if findings:
            self.fail(message(findings))

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
        self.assertFalse(fails('It is a bot account.', allowed=['a bot account']))
        self.assertTrue(fails('The bot is a bot account.', allowed=['a bot account']))
        for text in ('Bot', 'Bots', 'Bot:', 'Bots,', '(bot)', 'bot\n', 'run coconut team add-bot Ada',
                     'coconut team new-bot', 'coconut team remove-bot Ada'):
            with self.subTest(text=text):
                self.assertTrue(fails(text))
        for name in ('bot_id', '/v1/bots', 'bots', 'bot', 'new-bot', 'bot.created', 'create-bot', 'bots:', 'the robot',
                     'both bottom', 'bot.read bots.addressed', 'm.bot = ?', 'SELECT id FROM bots',
                     'INSERT OR IGNORE INTO team_shares (room, bot, entry) VALUES (?, ?, ?)'):
            with self.subTest(name=name):
                self.assertFalse(fails(name))

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


if __name__ == '__main__':
    unittest.main()
