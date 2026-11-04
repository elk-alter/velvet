"""Relationship identities must not require full display rows or change on optimization."""
import unittest
from unittest.mock import patch
import test_features as fixtures


class IdentityPresentationTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp
    tearDown=fixtures.FeatureTests.tearDown

    def test_inference_does_not_compute_display_only_fields(self):
        row={'path':str(self.root/'DEMO-001 part2 VR.mp4'),'code':'DEMO-001','height':2160,'codec':'h265','amane_snapshot':'{}'}
        expected=self.app.CORE.infer(row)
        with patch.object(self.app.CORE,'present',side_effect=AssertionError('Full display called for identity')):
            self.assertEqual(self.app.CORE.infer(row),expected)

    def test_identity_traits_agree_with_public_display_across_filename_and_metadata_cases(self):
        cases=[('DEMO-001 part2 VR.mp4','{}','auto'),('folder uncensored/VIDEO 第3集.mkv','{}','auto'),('censored/Disc01.mp4','{"content_type":"uncensored","vr":true}','auto'),('VRKM-123.mp4','{}','auto'),('censored uncensored/title.mp4','broken','auto'),('Film.mp4','{"content_type":"censored"}','unknown')]
        for relative,snapshot,override in cases:
            with self.subTest(relative=relative):
                row={'path':str(self.root/relative),'title':'Manual title','source':'filename','manual':1,'locked_fields':'["title"]','height':4320,'amane_snapshot':snapshot,'censor_override':override}
                shown=self.app.presentation(row)
                traits=self.app.identity_presentation(row)
                self.assertEqual({key:shown[key] for key in traits},traits)
                self.assertEqual(shown['title'],'Manual title')
                self.assertEqual(shown['resolution_label'],'8K')
