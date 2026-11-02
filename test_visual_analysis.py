import time
import unittest
from unittest.mock import Mock
import test_file_review as fixtures
from visual_analysis import watermark_findings, temporal_findings


def frame(at, text, y=.1, score=.95):
    return {'at': at, 'frame': at, 'text': [{'text': text, 'confidence': score, 'box': [.15, y, .2, .05]}]}


class VisualRuleTests(unittest.TestCase):
    def test_persistent_corner_text_has_evidence(self):
        result = watermark_findings([frame(i, 'DEMO.EXAMPLE.COM') for i in range(8)])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['code'], 'suspected_watermark')
        self.assertEqual(len(result[0]['samples']), 8)
        self.assertLessEqual(result[0]['confidence'], .95)

    def test_ordinary_subtitles_low_confidence_and_sparse_text_are_excluded(self):
        self.assertFalse(watermark_findings([frame(i, 'Repeated subtitle', .9) for i in range(8)]))
        self.assertFalse(watermark_findings([frame(i, 'DEMO TEXT', .1, .3) for i in range(8)]))
        self.assertFalse(watermark_findings([frame(i, 'TEXT ' + str(i)) for i in range(8)]))

    def test_multiple_same_frame_detections_do_not_fake_persistence(self):
        item = frame(0, 'EXAMPLE.COM')
        item['text'] *= 8
        self.assertFalse(watermark_findings([item] + [{'at': i, 'frame': i, 'text': []} for i in range(1, 8)]))

    def test_black_freeze_intervals_are_offset_and_uncertain(self):
        result = temporal_findings('black_start:1 black_end:4 black_duration:3\nfreeze_start: 5\nfreeze_end: 12', 100, 15, 0)
        self.assertEqual({r['code'] for r in result}, {'suspected_black', 'suspected_freeze'})
        self.assertEqual(result[0]['samples'][0]['at'], 101)
        self.assertTrue(all(r['confidence'] < 1 for r in result))
        self.assertFalse(temporal_findings('black_start:0 black_end:1 black_duration:1', 0, 15, 0))


class AutomaticReviewTests(unittest.TestCase):
    setUp = fixtures.FileReviewTests.setUp
    db = fixtures.FileReviewTests.db
    snapshot = fixtures.FileReviewTests.snapshot
    wait = fixtures.FileReviewTests.wait

    def prepare(self, findings):
        self.review.auto_settings = lambda: {'enabled': True, 'available': True}
        self.review.inspect = lambda row, mode: ('passed', 'sample fixture')
        self.review.analyze = lambda row: {'findings': findings, 'summary': 'fixture', 'generation': 'a' * 32}

    def test_automatic_results_can_be_reviewed_without_overwriting_manual_flags(self):
        self.prepare(watermark_findings([frame(i, 'DEMO.EXAMPLE.COM') for i in range(8)]))
        self.review.target(0)
        self.review.mark([1], 'replace', note='Manual decision')
        self.review.enqueue([1], 'auto'); self.wait()
        self.assertEqual(self.snapshot()['files'][1]['auto_status'], 'done')
        self.review.review_automatic([1], 'suspected_watermark', 'dismissed')
        record = self.snapshot()['files'][1]
        self.assertEqual({i['code'] for i in record['issues']}, {'replace'})
        self.assertEqual(record['note'], 'Manual decision')
        self.review.review_automatic([1], 'suspected_watermark', 'confirmed')
        self.assertTrue(any(i['source'] == 'automatic_confirmed' for i in self.snapshot()['files'][1]['issues']))

    def test_analysis_failure_is_not_a_clean_result(self):
        self.prepare(watermark_findings([frame(i, 'DEMO.EXAMPLE.COM') for i in range(8)]))
        self.review.enqueue([1], 'auto'); self.wait()
        self.review.analyze = Mock(side_effect=ValueError('OCR fixture failure'))
        self.review.enqueue([1], 'auto'); self.wait()
        record = self.snapshot()['files'][1]
        self.assertEqual(record['auto_status'], 'error')
        self.assertIn('OCR fixture failure', record['auto_error'])
        self.assertTrue(record['automatic_findings'])

    def test_clean_analysis_preserves_manual_watermark_and_revision_invalidates(self):
        self.prepare([])
        self.review.mark([1], 'watermark')
        self.review.enqueue([1], 'auto'); self.wait()
        self.assertTrue(any(i['source'] == 'manual' for i in self.snapshot()['files'][1]['issues']))
        with self.db() as c:
            c.execute('UPDATE videos SET mtime=mtime+5')
        self.assertFalse(self.snapshot()['files'][1]['automatic_findings'])
        self.assertEqual(self.snapshot()['files'][1]['auto_status'], '')

    def test_background_tick_only_queues_one_settled_file(self):
        with self.db() as c:
            c.execute('ALTER TABLE videos ADD COLUMN missing INTEGER DEFAULT 0')
            c.execute('UPDATE videos SET mtime=?', (time.time() - 200,))
        self.review.auto_settings = lambda: {'enabled': True, 'available': True}
        self.review.enqueue = Mock()
        self.review.auto_tick()
        self.review.enqueue.assert_called_once_with([1], 'auto')

    def test_invalid_review_and_disabled_background(self):
        with self.assertRaises(ValueError):
            self.review.review_automatic([1], 'arbitrary', 'confirmed')
        self.review.auto_settings = lambda: {'enabled': False, 'available': True}
        self.review.enqueue = Mock()
        self.review.auto_tick()
        self.review.enqueue.assert_not_called()


if __name__ == '__main__':
    unittest.main()
