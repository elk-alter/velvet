"""Full snapshots preserve user decisions, evidence, policies and queue provenance."""
import unittest
from full_backup import create,restore
from scripts import upgrade_fixture as fixture
import test_features as fixtures


class UpgradePreservationTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp
    tearDown=fixtures.FeatureTests.tearDown

    def test_full_rollback_restores_both_databases_images_and_restart_semantics(self):
        source=self.root/'synthetic';source.mkdir()
        (source/'Example.mp4').write_bytes(b'synthetic source')
        fixture.seed(self.app,source)
        data=self.app.DATA;protected=fixture.snapshot(data)
        images=fixture.files(data);cache=fixture.cache_snapshot(data)
        configuration={name:(data/name).read_bytes() for name in ('settings.json','tag_rules.json')}
        archive=self.root/'snapshot.zip';create(data,archive,include_cache=True)
        self.app.ARTWORK.ensure(recover=True);self.app.REVIEW.ensure(recover=True)
        self.app.BUILTIN_ENGINES.clear()
        fixture.start_without_workers(self.app)
        fixture.drain_builtin_with_mock(self.app)
        for name in images:(data/name).write_bytes(b'synthetic changed image')
        (data/'settings.json').write_text('{}')
        with self.app.db() as c:
            c.execute('UPDATE videos SET favorite=0,rating=0')
            c.execute("UPDATE file_reviews SET auto_overrides='{}',decision='open'")
        rollback=restore(data,archive)
        self.assertTrue(rollback['rollback'])
        self.assertEqual(fixture.snapshot(data),protected)
        self.assertEqual(fixture.cache_snapshot(data),cache)
        self.assertEqual(fixture.files(data),images)
        self.assertEqual({name:(data/name).read_bytes() for name in configuration},configuration)
        self.app.ARTWORK.ensure(recover=True);self.app.REVIEW.ensure(recover=True)
        self.app.BUILTIN_ENGINES.clear()
        fixture.start_without_workers(self.app)
        fixture.assert_protected(self.app,protected,images)
        fixture.drain_builtin_with_mock(self.app)
