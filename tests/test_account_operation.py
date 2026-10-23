from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.cloud.run_account_operation import prepare_operation


class OperationFileTests(unittest.TestCase):
    def test_file_contents_and_message_count_can_change_between_runs(self):
        with TemporaryDirectory() as folder:
            file = Path(folder) / 'settings.txt'
            file.write_text('#time=0|', encoding='utf-8')
            first, count = prepare_operation(file)
            self.assertEqual(count, 1)
            file.write_text('#time=1|#time=2|', encoding='utf-8')
            second, count = prepare_operation(file)
            self.assertEqual(count, 2)
            self.assertEqual(len(second), 10)
            self.assertTrue(all(row['repetitions'] == 1 for row in second))
            self.assertNotEqual(first[0]['steps'], second[0]['steps'])

    def test_invalid_or_empty_file_fails_before_any_login(self):
        with TemporaryDirectory() as folder:
            file = Path(folder) / 'settings.txt'
            for text in ['', '#send=invalid|']:
                file.write_text(text, encoding='utf-8')
                with self.assertRaises(ValueError):
                    prepare_operation(file)
