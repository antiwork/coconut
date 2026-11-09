import struct
import unittest
from io import BytesIO
from PIL import Image

import test_native_execution as native_tests
from coconut.service import ServiceError, YieldForDecision

def image_bytes(format='PNG'):
    output = BytesIO()
    Image.new('RGB', (20, 30), 'white').save(output, format=format)
    return output.getvalue()


PNG = image_bytes()


class DecisionPreviewTests(unittest.TestCase):
    setUp = native_tests.NativeExecutionTests.setUp
    tearDown = native_tests.NativeExecutionTests.tearDown
    write = native_tests.NativeExecutionTests.write
    event = native_tests.NativeExecutionTests.event
    restart = native_tests.NativeExecutionTests.restart

    def image(self, name='preview.png', content=PNG):
        path = self.root / name
        path.write_bytes(content)
        return dict(path=str(path), caption='Mobile form')

    def question(self, previews):
        with self.assertRaises(YieldForDecision):
            self.event('native_question', dict(title='Approve this form?', context='Review the form.',
                options=[dict(id='approve', label='Approve appearance'), dict(id='revise', label='Revise appearance')], previews=previews))
        return self.service.snapshot()['decisions'][-1]

    def test_preview_bytes_survive_source_change_and_restart_without_public_paths(self):
        item = self.image()
        decision = self.question([item])
        self.root.joinpath('preview.png').write_bytes(b'changed')
        self.restart()
        saved = self.service.snapshot()['decisions'][-1]
        self.assertEqual(saved['previews'], decision['previews'])
        self.assertEqual(self.service.artifact_content(saved['previews'][0]['artifact_id'])[0], PNG)
        self.assertNotIn('path', saved['previews'][0])
        self.assertFalse(self.service.snapshot()['artifacts'])
        self.assertNotIn('_content_base64', str(saved))

    def test_approval_requires_current_images_and_revision(self):
        decision = self.question([self.image()])
        path = f"/v1/decisions/{decision['id']}/answer"
        for extra in ({}, {'previews_hash': 'old'}):
            with self.assertRaises(ServiceError):
                self.write('POST', path, dict(revision=1, option_id='approve', **extra))
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')
        result = self.write('POST', path, dict(revision=1, option_id='approve', previews_hash=decision['previews_hash']))
        self.assertEqual(result['status'], 'approved')

    def test_revision_preserves_omitted_previews_and_replaces_explicit_previews(self):
        decision = self.question([self.image()])
        task = self.service._get('tasks', decision['task_id'])
        revision = dict(title=decision['title'], options=decision['options'])
        with self.service.transaction():
            self.service._revise_question(decision, task, revision, 'Shorten the context')
        self.assertEqual(decision['previews'][0]['caption'], 'Mobile form')
        old_hash = decision['previews_hash']
        with self.service.transaction():
            self.service._revise_question(decision, task, dict(revision, previews=[dict(self.image(), caption='Desktop form')]), 'Replace preview')
        self.assertNotEqual(decision['previews_hash'], old_hash)
        with self.assertRaises(ServiceError):
            self.write('POST', f"/v1/decisions/{decision['id']}/answer", dict(revision=decision['revision'], option_id='approve', previews_hash=old_hash))
        with self.service.transaction():
            self.service._revise_question(decision, task, dict(revision, previews=[]), 'Remove visual review')
        self.assertEqual(decision['previews'], [])
        self.assertIsNone(decision['previews_hash'])

    def test_invalid_images_roll_back_the_whole_question(self):
        valid = self.image()
        invalid = self.image('secret.txt', b'not an image')
        with self.assertRaises(ServiceError):
            self.event('native_question', dict(title='Review?', options=[dict(id='yes', label='Yes')], previews=[valid, invalid]))
        self.assertFalse(self.service._all('decisions'))
        self.assertFalse(self.service._all('artifacts'))
        self.assertEqual(self.service._get('tasks', self.task['id'])['status'], 'working')

    def reply_context(self, decision, text):
        self.write('POST', f"/v1/decisions/{decision['id']}/reply", dict(text=text, revision=decision['revision']))
        context = self.service._all('contexts')[-1]
        with self.service.transaction():
            context['status'] = 'working'
            self.service._put('contexts', context)
        return context

    def test_typed_visual_approval_keeps_the_card_pending_with_a_clear_instruction(self):
        decision = self.question([self.image()])
        context = self.reply_context(decision, 'Looks good')
        result = self.service.resolve_decision_reply(context['id'], dict(choice='approve', option_id='approve', quote='Looks good'))
        self.assertEqual(result['status'], 'pending')
        self.assertIn('select a choice on the card', result['error'])
        self.assertEqual(self.service._get('decisions', decision['id'])['status'], 'pending')

    def test_draft_reply_replaces_and_removes_previews_atomically(self):
        draft = dict(channel='Gmail', sender='me@example.com', recipient='sarah@example.com',
                     subject='Review', body='Please review the form.')
        self.event('native_proposal', dict(key='draft', title='Send the review request?', draft=draft, previews=[self.image()]))
        decision = self.service.snapshot()['decisions'][-1]
        for previews in ([dict(self.image(), caption='New form')], []):
            old_hash = decision['previews_hash']
            context = self.reply_context(decision, 'Revise this draft')
            result = self.service.resolve_decision_reply(context['id'], dict(choice='modify', quote='Revise this draft',
                revision=dict(draft=dict(draft, body='Please review the updated form.'), previews=previews)))
            decision = result['decision']
            self.assertEqual(decision['status'], 'pending')
            self.assertNotEqual(decision['previews_hash'], old_hash)
            self.assertEqual(len(decision['previews']), len(previews))

    def test_rejects_truncated_png_and_jpeg_before_pausing(self):
        jpeg = image_bytes('JPEG')
        for content in (PNG[:33], PNG[:-20], jpeg[:len(jpeg) // 2], jpeg[:-10]):
            with self.subTest(size=len(content)), self.assertRaises(ServiceError):
                self.event('native_question', dict(title='Review?', options=[dict(id='yes', label='Yes')],
                    previews=[self.image(content=content)]))
        self.assertFalse(self.service._all('artifacts'))
        self.assertFalse(self.service._all('decisions'))
        decision = self.question([self.image(content=jpeg)])
        self.assertEqual(decision['previews'][0]['width'], 20)
        self.assertEqual(self.service.artifact_content(decision['previews'][0]['artifact_id'])[0], jpeg)

    def test_a_webp_photo_is_stored_as_a_jpeg(self):
        # Listing and shop sites often serve WebP. Every app shows the JPEG.
        decision = self.question([self.image(name='listing.webp', content=image_bytes('WEBP'))])
        preview = decision['previews'][0]
        self.assertEqual((preview['width'], preview['height']), (20, 30))
        with Image.open(BytesIO(self.service.artifact_content(preview['artifact_id'])[0])) as stored:
            self.assertEqual((stored.format, stored.size), ('JPEG', (20, 30)))
        artifact = self.service._get('artifacts', preview['artifact_id'])
        self.assertEqual(artifact['title'], 'preview.jpg')

    def test_rejects_symlinks_large_dimensions_and_too_many_images(self):
        valid = self.image()
        link = self.root / 'link.png'; link.symlink_to(valid['path'])
        huge = bytearray(PNG); huge[16:24] = struct.pack('>II', 16000, 16000)
        invalid_sets = [[dict(valid, path=str(link))], [self.image('huge.png', huge)], [valid] * 7]
        for previews in invalid_sets:
            with self.subTest(previews=len(previews)), self.assertRaises(ServiceError):
                self.event('native_question', dict(title='Review?', options=[dict(id='yes', label='Yes')], previews=previews))
        self.assertFalse(self.service._all('artifacts'))


if __name__ == '__main__':
    unittest.main()
