"""Check the fallback models of a profile with the pinned Hermes fallback chain."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from hermes_env import needs_hermes, PYTHON, SOURCE


@needs_hermes
class NativeAPIFallbackTests(unittest.TestCase):
    def test_native_config_keeps_entries_primary_and_other_keys(self):
        with tempfile.TemporaryDirectory() as root:
            code = '''
import json, os
from pathlib import Path
from coconut.native_fallback import save, state
from coconut.native_settings import raw_config
root=Path(os.environ['HERMES_HOME'])
private={'provider':'custom-route','model':'Private-Model','base_url':'${OTHER_URL}','key_env':'OTHER_KEY'}
original={'model': {'default':'primary-model','provider':'openrouter'},
          'fallback_providers': [private],
          'fallback_model': {'provider':'anthropic','model':'legacy-model','api_key':'inline-secret'},
          'terminal': {'cwd':'/tmp'}}
(root/'config.yaml').write_text(json.dumps(original))
(root/'.env').write_text('OTHER_KEY=profile-secret\\nOTHER_URL=https://other.example/v1\\n')

# The state is the whole chain, in order, with only the model and its provider.
listed=state(root)
assert listed=={'models':[{'model':'Private-Model','provider':'custom-route'},
                          {'model':'legacy-model','provider':'anthropic'}]}, listed
for text in ('profile-secret','inline-secret','OTHER_KEY','OTHER_URL','other.example','key_env','api_key','base_url'):
    assert text not in json.dumps(listed), text

# A new model goes first. The existing entries keep each key. The legacy key merges into fallback_providers.
result=save(root, [{'model':'new-model','provider':'openrouter'},
                   {'model':'private-model','provider':'Custom-Route'},
                   {'model':'legacy-model','provider':'anthropic'}])
assert [item['model'] for item in result['models']]==['new-model','Private-Model','legacy-model'], result
saved=raw_config(root)
assert saved['model']==original['model'] and saved['terminal']==original['terminal']
assert 'fallback_model' not in saved
assert saved['fallback_providers'][0]=={'model':'new-model','provider':'openrouter'}
assert saved['fallback_providers'][1]==private, saved['fallback_providers'][1]
assert saved['fallback_providers'][2]['api_key']=='inline-secret'
assert 'profile-secret' not in (root/'config.yaml').read_text()
assert 'inline-secret' not in json.dumps(result) and 'OTHER_KEY' not in json.dumps(result)

# Hermes reads the same chain back.
from hermes_cli.fallback_config import get_fallback_chain
assert [entry['model'] for entry in get_fallback_chain(saved)]==['new-model','Private-Model','legacy-model']

# A later model with the same route is dropped. The first one keeps its keys.
result=save(root, [{'model':'private-model','provider':'custom-route'},{'model':'PRIVATE-MODEL','provider':' Custom-Route '},
                   {'model':'new-model','provider':'openrouter'}])
assert [item['model'] for item in result['models']]==['Private-Model','new-model'], result
saved=raw_config(root)
assert saved['fallback_providers']==[private, {'model':'new-model','provider':'openrouter'}], saved['fallback_providers']

# A bad body is an error, and the file does not change.
before=(root/'config.yaml').read_text()
for models in ([{'model':'new-model','provider':'openrouter','base_url':'https://x.example'}], 'new-model',
               [{'model':'','provider':'openrouter'}]):
    try:
        save(root, models)
    except ValueError:
        pass
    else:
        raise AssertionError(models)
assert (root/'config.yaml').read_text()==before

# One provider and model with two endpoints is two entries for Hermes. A save keeps each one with its endpoint.
one={'provider':'openrouter','model':'twin-model','base_url':'https://one.example/v1'}
two={'provider':'openrouter','model':'twin-model','base_url':'https://two.example/v1'}
(root/'config.yaml').write_text(json.dumps(dict(original, fallback_providers=[one, two], fallback_model=None)))
twin={'model':'twin-model','provider':'openrouter'}
assert save(root, [twin, twin])=={'models':[twin, twin]}
assert raw_config(root)['fallback_providers']==[one, two], raw_config(root)['fallback_providers']
# The app removes the first fallback model. The second entry stays, with its own endpoint.
assert save(root, [twin])=={'models':[twin]}
assert raw_config(root)['fallback_providers']==[two], raw_config(root)['fallback_providers']

# An empty chain removes both fallback keys.
assert save(root, [])=={'models':[]}
saved=raw_config(root)
assert 'fallback_providers' not in saved and 'fallback_model' not in saved
assert saved['model']==original['model'] and saved['terminal']==original['terminal']
assert state(root)=={'models':[]}
print('Native fallback settings passed')
'''
            env = dict(os.environ, HERMES_HOME=root,
                       PYTHONPATH=os.pathsep.join([str(Path(SOURCE).resolve()), str(Path('backend').resolve())]))
            result = subprocess.run([PYTHON, '-c', code], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Native fallback settings passed', result.stdout)
