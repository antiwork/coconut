"""Check release versions passed to Xcode without signing or uploading."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import iphone


class ReleaseVersionTests(unittest.TestCase):
    def test_archive_uses_current_utc_date_and_keeps_build_counter(self):
        config = dict(team='TESTTEAM00', version='date')
        with patch.object(iphone.shutil, 'which', return_value='/usr/bin/xcodegen'), \
                patch.object(iphone, 'run'), patch.object(iphone, 'save_config'), \
                patch.object(iphone, 'verify_app'), patch.object(iphone, 'xcode') as xcode, \
                patch.object(iphone, 'next_build_number', side_effect=[89, 90]), \
                patch.object(iphone, 'datetime') as clock:
            for day, number in [(13, 89), (14, 90)]:
                clock.now.return_value = datetime(2026, 9, day, tzinfo=timezone.utc)
                iphone.build(config, archive=True)
                arguments = xcode.call_args.args[0]
                self.assertIn(f'MARKETING_VERSION=2026.9.{day}', arguments)
                self.assertIn(f'CURRENT_PROJECT_VERSION={number}', arguments)
                clock.now.assert_called_with(timezone.utc)
                self.assertEqual(config['version'], 'date')

    def test_explicit_version_stays_fixed(self):
        config = dict(team='TESTTEAM00', version='2.3.4')
        with patch.object(iphone.shutil, 'which', return_value='/usr/bin/xcodegen'), \
                patch.object(iphone, 'run'), patch.object(iphone, 'save_config'), \
                patch.object(iphone, 'verify_app'), patch.object(iphone, 'xcode') as xcode, \
                patch.object(iphone, 'next_build_number', return_value=91):
            iphone.build(config, archive=True)
            self.assertIn('MARKETING_VERSION=2.3.4', xcode.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
