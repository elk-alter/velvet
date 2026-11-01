import json,sqlite3,zipfile,hashlib
import test_features as base
class P0Tests(base.FeatureTests):
 def fixture(self):
  a=self.add('ABC-1-1080p.mp4',b'1080','ABC-1');b=self.add('ABC-1-4K-part1.mp4',b'4k1','ABC-1');c=self.add('ABC-1-4K-part2.mp4',b'4k2','ABC-1')
  with self.app.db() as db:
   db.execute('UPDATE videos SET height=1080,duration=600,codec="h264" WHERE id=?',(a,));db.execute('UPDATE videos SET height=2160,duration=300,codec="hevc" WHERE id IN (?,?)',(b,c))
  return a,b,c
 def test_p0_hierarchy_and_resume(self):
  a,b,c=self.fixture();core=self.app.CORE;movies=core.hierarchy();self.assertEqual(len(movies),1);self.assertEqual(len(movies[0]['_versions']),2);self.assertEqual(movies[0]['duration'],600);self.assertEqual(len(movies[0]['_parts']),2)
  core.prefer(a);self.assertEqual(core.hierarchy()[0]['id'],a)
  core.prefer(b)
  with self.app.db() as db:db.execute('UPDATE videos SET watched=1 WHERE id=?',(b,));db.execute('UPDATE videos SET position=72 WHERE id=?',(c,))
  movie=core.hierarchy()[0];self.assertEqual(movie['id'],c);self.assertEqual(movie['position'],72);self.assertFalse(movie['watched'])
  with self.app.db() as db:db.execute('UPDATE videos SET watched=1 WHERE id=?',(c,))
  self.assertTrue(core.hierarchy()[0]['watched'])
 def test_p0_relations_reversible(self):
  a,b,c=self.fixture();core=self.app.CORE;before=core.hierarchy()
  core.relation([{'id':c,'movie_key':'separate','version_key':'separate-v','part_no':1}]);self.assertEqual(len(core.hierarchy()),2)
  core.undo();self.assertEqual(len(core.hierarchy()),1)
  links=core.hierarchy()[0]['_versions'];v=next(v for v in links if len(v['_parts'])==2)
  with self.assertRaises(ValueError):core.relation([{'id':c,'movie_key':v['movie_key'],'version_key':v['version_key'],'part_no':1}])
  self.assertEqual(len(core.hierarchy()),1)
 def test_p0_backup_restore_preferences(self):
  a,b,c=self.fixture();core=self.app.CORE;core.prefer(a)
  cfg=self.app.config();cfg['tag_aliases']={'a':'b'};(self.app.DATA/'settings.json').write_text(json.dumps(cfg),'utf-8')
  with self.app.db() as db:db.execute('INSERT INTO actor_preferences VALUES(?,?,?)',('Name',5,1))
  backup=core.backup();self.assertTrue(core.validate_backup(backup).is_file())
  core.prefer(b)
  with self.app.db() as db:db.execute('UPDATE actor_preferences SET rating=1')
  rollback=core.restore(backup);self.assertNotEqual(rollback,backup);self.assertEqual(core.hierarchy()[0]['id'],a)
  with self.app.db() as db:self.assertEqual(db.execute('SELECT rating FROM actor_preferences').fetchone()[0],5)
  self.assertEqual((self.root/'ABC-1-4K-part1.mp4').read_bytes(),b'4k1')
  corrupt=self.app.DATA/'backups'/'bad.zip'
  with zipfile.ZipFile(corrupt,'w') as z:z.writestr('library.db',b'invalid');z.writestr('manifest.json','{"schema":2}')
  with self.assertRaises(ValueError):core.validate_backup('bad.zip')
 def test_p0_duplicate_preference(self):
  a,b,c=self.fixture()
  with self.app.db() as db:db.execute('INSERT INTO duplicate_choices VALUES(?,?)',('candidate:ABC-1',a))
  self.assertEqual(self.app.CORE.hierarchy()[0]['id'],a)
 def test_p0_same_quality_alternatives_not_parts(self):
  a=self.add('ABC-1.mp4',b'a','ABC-1');b=self.add('ABC-1.mkv',b'b','ABC-1');m=self.app.CORE.hierarchy()[0];self.assertEqual(len(m['_versions']),2);self.assertTrue(all(len(v['_parts'])==1 for v in m['_versions']))
 def test_p0_http_restore_and_cancel(self):
  import threading
  from urllib.request import Request,urlopen
  a,b,c=self.fixture();app=self.app
  server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  def post(path,data):
   with urlopen(Request('http://127.0.0.1:'+str(server.server_port)+path,data=json.dumps(data).encode(),headers={'Host':'127.0.0.1:8765','X-Velvet-Token':app.TOKEN,'Content-Type':'application/json'})) as r:return json.load(r)
  try:
   app.CORE.prefer(b);original_version=app.CORE.hierarchy()[0]['version_key']
   post('/api/duplicates-choice',{'key':'candidate:ABC-1','preferred_id':a});self.assertEqual(app.CORE.hierarchy()[0]['id'],a)
   post('/api/duplicates-choice',{'key':'candidate:ABC-1','preferred_id':None});self.assertEqual(app.CORE.hierarchy()[0]['version_key'],original_version)
   backup=post('/api/backup',{})['name'];self.assertTrue(post('/api/backup-validate',{'name':backup})['ok'])
   post('/api/movie-flag',{'id':b,'field':'favorite','value':1})
   with app.db() as db:self.assertEqual(db.execute('SELECT SUM(favorite) FROM videos').fetchone()[0],3)
   post('/api/movie-flag',{'id':b,'field':'watched','value':1})
   with app.db() as db:self.assertEqual(db.execute('SELECT SUM(watched) FROM videos').fetchone()[0],2)
   post('/api/edit',{'id':a,'rating':5});post('/api/restore',{'name':backup,'confirm':backup})
   with app.db() as db:self.assertEqual(db.execute('SELECT rating FROM videos WHERE id=?',(a,)).fetchone()[0],0)
   post('/api/task-cancel',{'kind':'scan'});self.assertTrue(app.SCAN_CANCEL.is_set())
   with app.db() as db:db.execute('INSERT INTO scrape_jobs(code,remote_id,status,created) VALUES(?,?,?,?)',('ABC-1',101,'queued',app.time.time()))
   requests=[]
   def remote(path,data=None):requests.append((path,data));return {'status':'cancelled','result':{}}
   app.amane_request=remote;post('/api/task-cancel',{'kind':'scrape','id':1});self.assertEqual(requests[0][0],'tasks/batch');self.assertEqual(requests[0][1]['task_ids'],[101])
   with app.db() as db:self.assertEqual(db.execute('SELECT status FROM scrape_jobs').fetchone()[0],'cancelled')
  finally:server.shutdown();server.server_close();thread.join()
 def test_p0_manual_tags_survive_nfo(self):
  a=self.add('ABC-1.mp4',b'video','ABC-1');(self.root/'ABC-1.nfo').write_text('<movie><title>Source</title><tag>RemoteTag</tag></movie>','utf-8')
  self.app.binary=lambda name:None
  with self.app.db() as db:
   db.execute('INSERT INTO folders VALUES(?)',(str(self.root),));db.execute('UPDATE videos SET tags=?,locked_fields=? WHERE id=?',('ManualTag','["tags"]',a))
  self.app.scan(True)
  with self.app.db() as db:self.assertEqual(db.execute('SELECT tags FROM videos WHERE id=?',(a,)).fetchone()[0],'ManualTag')
if __name__=='__main__':
 import unittest;unittest.main()
