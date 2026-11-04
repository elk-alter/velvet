"""Relationship identities must not require full display rows or change on optimization."""
import unittest
from itertools import product
from media_presentation import presentation
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

    def test_browse_projection_preserves_title_protection_and_display_traits(self):
        from media_presentation import projection_presentation
        paths=['Collection/VIDEO.mp4','Film part2 VR.mp4','uncensored/Disc01.mkv','censored uncensored/File.mp4']
        for relative,source,manual,locks,snapshot,amane_id in product(paths,['filename','nfo',''],[0,1],['[]','["title"]'],['{}','{"vr":true,"content_type":"censored"}'],[0,12]):
            row={'path':str(self.root/relative),'title':'VIDEO','source':source,'manual':manual,'locked_fields':locks,'amane_snapshot':snapshot,'height':2160,'amane_id':amane_id}
            original=dict(row);full=presentation(row);summary=projection_presentation(row)
            self.assertEqual(summary,{key:full[key] for key in ('title','vr','censorship','resolution_label')})
            self.assertEqual(row,original)

    def test_application_browse_does_not_generate_full_detail_fields(self):
        with self.app.db() as c:
            c.execute('INSERT INTO videos(path,title,code,height) VALUES(?,?,?,?)',(str(self.root/'Film.mp4'),'Synthetic title','DEMO-1',1080))
        with patch.object(self.app.CORE,'present',side_effect=AssertionError('Full detail presentation in list projection')):
            item=self.app.BROWSE.query({'collections':'off'})['items'][0]
        self.assertEqual(item['title'],'Synthetic title')
        self.assertEqual(item['resolution_label'],'1080p')

    def test_legacy_query_constructor_keeps_custom_presentation(self):
        from library_query import LibraryQuery
        with self.app.db() as c:
            c.execute('INSERT INTO videos(path,title,code,height) VALUES(?,?,?,?)',(str(self.root/'Film.mp4'),'Original title','DEMO-1',1080))
        original=self.app.CORE.present
        def custom(row):
            result=original(row);result['title']='Custom display';return result
        query=LibraryQuery(self.app.db,self.app.CORE,self.app.LIBRARIES,self.app.DIRECTORY_POLICY)
        with patch.object(self.app.CORE,'present',side_effect=custom) as presented:
            item=query.query({'collections':'off'})['items'][0]
        self.assertEqual(item['title'],'Custom display')
        self.assertEqual(presented.call_count,1)
