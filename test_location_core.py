import unittest
from pathlib import Path
import test_features as fixtures
from media_locations import MediaLocations


class LocationCoreTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp
    tearDown=fixtures.FeatureTests.tearDown
    add=fixtures.FeatureTests.add

    def test_location_switch_keeps_auto_relationships_and_explicit_reset_is_reversible(self):
        vid=self.add('Local-part1.mp4',b'content','')
        self.app.CORE.reconcile()
        with self.app.db() as c:before=tuple(c.execute('SELECT * FROM media_links WHERE video_id=?',(vid,)).fetchone())
        locations=MediaLocations(self.app.db,lambda path:'volume');locations.migrate()
        target=self.root/'Another-part99.mp4';target.write_bytes(b'content')
        loc=locations.bind_verified_copy(vid,target,'volume','task');locations.prefer(vid,loc)
        self.app.CORE.reconcile()
        with self.app.db() as c:self.assertEqual(tuple(c.execute('SELECT * FROM media_links WHERE video_id=?',(vid,)).fetchone()),before)
        self.app.CORE.reset([vid])
        with self.app.db() as c:self.assertEqual(c.execute('SELECT part_no FROM media_links WHERE video_id=?',(vid,)).fetchone()[0],99)
        self.app.CORE.undo()
        with self.app.db() as c:self.assertEqual(tuple(c.execute('SELECT * FROM media_links WHERE video_id=?',(vid,)).fetchone()),before)

    def test_forgetting_video_does_not_reassign_old_file_uuid_or_relationship(self):
        vid=self.add('original.mp4',b'old','');self.app.CORE.reconcile()
        locations=MediaLocations(self.app.db,lambda path:'volume');locations.migrate();old=locations.asset(vid)['id']
        with self.app.db() as c:
            c.execute("UPDATE media_links SET manual=1,movie_key='manual-old' WHERE video_id=?",(vid,))
            c.execute('DELETE FROM videos WHERE id=?',(vid,))
            c.execute('INSERT INTO videos(id,path,title,size,mtime) VALUES(?,?,?,?,?)',(vid,str(self.root/'different.mp4'),'different',3,1))
        locations.migrate();self.app.CORE.reconcile()
        self.assertNotEqual(locations.asset(vid)['id'],old)
        with self.app.db() as c:
            self.assertIsNone(c.execute('SELECT video_id FROM media_assets WHERE id=?',(old,)).fetchone()[0])
            self.assertNotEqual(c.execute('SELECT movie_key FROM media_links WHERE video_id=?',(vid,)).fetchone()[0],'manual-old')

    def test_forgotten_identity_does_not_block_same_path_reimport(self):
        from media_locations import MediaLocations
        vid=self.add('Same-part1.mp4',b'original','')
        locations=MediaLocations(self.app.db,lambda path:'volume');locations.migrate()
        old=locations.asset(vid)['id']
        with self.app.db() as c:
            row=dict(c.execute('SELECT * FROM videos WHERE id=?',(vid,)).fetchone())
            c.execute('DELETE FROM videos WHERE id=?',(vid,))
            c.execute('INSERT INTO videos('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        locations.migrate()
        self.assertNotEqual(locations.asset(vid)['id'],old)
        self.assertEqual(len(locations.locations(vid)),1)

    def test_location_switch_carries_current_quality_evidence_but_not_stale_evidence(self):
        from media_locations import MediaLocations
        import os
        vid=self.add('Quality.mp4',b'synthetic','')
        with self.app.db() as c:
            row=dict(c.execute('SELECT * FROM videos WHERE id=?',(vid,)).fetchone())
            c.execute("INSERT INTO file_reviews(video_id,size,mtime,flags,note) VALUES(?,?,?,?,?)",(vid,row['size'],row['mtime'],'["watermark"]','manual decision'))
        locations=MediaLocations(self.app.db,lambda path:'volume');locations.migrate()
        target=Path(row['path']).parent/'copy.mp4';target.write_bytes(Path(row['path']).read_bytes())
        os.utime(target,(1600000000,1600000000))
        location=locations.bind_verified_copy(vid,target,'volume','test')
        locations.prefer(vid,location)
        with self.app.db() as c:
            video=dict(c.execute('SELECT * FROM videos WHERE id=?',(vid,)).fetchone())
            review=dict(c.execute('SELECT * FROM file_reviews WHERE video_id=?',(vid,)).fetchone())
            self.assertEqual((video['size'],video['mtime']),(review['size'],review['mtime']))
            self.assertEqual(review['note'],'manual decision')
            c.execute('UPDATE file_reviews SET mtime=1 WHERE video_id=?',(vid,))
        locations.prefer(vid,location)
        with self.app.db() as c:self.assertEqual(c.execute('SELECT mtime FROM file_reviews WHERE video_id=?',(vid,)).fetchone()[0],1)
