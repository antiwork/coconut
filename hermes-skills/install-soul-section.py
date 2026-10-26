"""Install one SOUL.md section into a bot's Hermes home on this Mac.

Usage: python3 hermes-skills/install-soul-section.py <bot folder> <section file> [<bots root>]
Example: python3 hermes-skills/install-soul-section.py desk hermes-skills/soul/when-to-ask-gianfranco.md

The section file starts with its "## " heading. When SOUL.md has that heading, the script replaces the section up to
the next "## " heading. Otherwise it appends the section. It backs up SOUL.md to ~/Coconut-backups/soul-<time>/ first.
"""
import os
from pathlib import Path
import re
import shutil
import sys
import time

here = Path(__file__).resolve().parent
folder, section = sys.argv[1], Path(sys.argv[2]).read_text().strip() + '\n'
root = Path(sys.argv[3]) if len(sys.argv) > 3 else Path.home() / 'Coconut/.runtime/host/bots'
bot = re.search(r'^bot_id: *(\S+)', (here / folder / 'BOT').read_text(), re.M).group(1)
soul = root / bot / 'home' / 'SOUL.md'
heading = section.splitlines()[0]
if not heading.startswith('## '):
    sys.exit('The section file must start with a "## " heading.')
text = soul.read_text()
pattern = re.compile(r'^' + re.escape(heading) + r'\n.*?(?=^## |\Z)', re.M | re.S)
if pattern.search(text):
    updated = pattern.sub(lambda match: section + ('\n' if match.end() < len(text) else ''), text, count=1)
else:
    updated = text.rstrip('\n') + '\n\n' + section
if updated == text:
    print('unchanged', heading, 'in', folder)
    sys.exit()
backup = Path.home() / 'Coconut-backups' / ('soul-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())) / bot
backup.mkdir(parents=True, exist_ok=True)
shutil.copy2(soul, backup / 'SOUL.md')
temporary = soul.with_name('.SOUL.md.coconut-new')
temporary.write_text(updated)
os.chmod(temporary, 0o600)
os.replace(temporary, soul)
print('installed', heading, 'in', folder, '- backup', backup / 'SOUL.md')
