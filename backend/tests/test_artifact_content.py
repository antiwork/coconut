"""The bytes of an attachment live in their own table, and older rows move there at the start."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from coconut.providers import FixtureProvider
from coconut.service import Service, ServiceError, digest


class ArtifactContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'coconut'
        self.service = self.open()
        self.bot = self.service.mutate('POST', '/v1/bots', {'name': 'Coder', 'role': ''}, str(uuid4()))

    def open(self):
        service = Service(self.root, FixtureProvider(self.root), start_workers=False)
        self.addCleanup(self.close, service)
        return service

    @staticmethod
    def close(service):
        if not getattr(service, 'closed_by_test', False):
            service.closed_by_test = True
            service.close()

    def upload(self, content=b'attached bytes'):
        return self.service.mutate('POST', '/v1/attachments', {'bot_id': self.bot['id'], 'filename': 'note.txt',
                                                               'content_base64': base64.b64encode(content).decode()}, str(uuid4()))

    def test_an_upload_keeps_its_bytes_out_of_the_artifact_row(self):
        artifact = self.upload()
        row = self.service.db.execute('SELECT data FROM artifacts WHERE id=?', (artifact['id'],)).fetchone()
        self.assertNotIn('_content_base64', json.loads(row[0]))
        self.assertEqual(self.service.artifact_content(artifact['id']), (b'attached bytes', 'note.txt'))
        self.assertEqual(base64.b64decode(self.service.artifact_base64(artifact['id'])), b'attached bytes')

    def test_an_older_row_moves_its_bytes_at_the_next_start(self):
        artifact = self.upload()
        # Rebuild the older shape: the bytes inline, and no row in the new table.
        with self.service.transaction():
            row = self.service._get('artifacts', artifact['id'])
            row['_content_base64'] = base64.b64encode(b'attached bytes').decode()
            self.service._put('artifacts', row)
            self.service.db.execute('DELETE FROM artifact_content WHERE id=?', (artifact['id'],))
        # The reader still finds the inline bytes before the move.
        self.assertEqual(self.service.artifact_content(artifact['id'])[0], b'attached bytes')
        self.close(self.service)
        reopened = self.open()
        row = json.loads(reopened.db.execute('SELECT data FROM artifacts WHERE id=?', (artifact['id'],)).fetchone()[0])
        self.assertNotIn('_content_base64', row)
        self.assertEqual(reopened.artifact_content(artifact['id'])[0], b'attached bytes')

    def test_an_artifact_without_bytes_falls_back_to_its_file(self):
        content = b'a file on the Mac'
        (self.service.files).mkdir(parents=True, exist_ok=True)
        (self.service.files / 'plain').write_bytes(content)
        with self.service.transaction():
            self.service._put('artifacts', dict(id='plain', bot_id=self.bot['id'], task_id=None, title='report.txt', kind='file',
                                                status='ready', preview='', url=None, created_at='2026-01-01T00:00:00Z',
                                                _content_hash=digest(content)))
        self.assertIsNone(self.service.artifact_bytes(self.service._get('artifacts', 'plain')))
        self.assertEqual(self.service.artifact_content('plain'), (content, 'report.txt'))
        (self.service.files / 'plain').write_bytes(b'changed')
        with self.assertRaises(ServiceError):
            self.service.artifact_content('plain')


if __name__ == '__main__':
    unittest.main()
