import unittest,tempfile,os,importlib.util,json
from pathlib import Path
class FeatureTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);os.environ['VELVET_DATA']=str(self.root/'data')
  spec=importlib.util.spec_from_file_location('features',Path(__file__).with_name('app.py'));self.app=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.app)
 def tearDown(self):self.tmp.cleanup()
 def add(self,name,content,code):
  p=self.root/name;p.write_bytes(content);st=p.stat()
  with self.app.db() as c:
   cur=c.execute('INSERT INTO videos(path,title,size,mtime,code) VALUES(?,?,?,?,?)',(str(p),name,len(content),st.st_mtime,code));return cur.lastrowid
 def test_exact_and_candidates(self):
  a=self.add('a.mp4',b'12345','ABC-1');b=self.add('b.mp4',b'12345','ABC-1');c=self.add('c.mp4',b'99999','ABC-1')
  self.app.detect_duplicates();groups=self.app.duplicate_groups();exact=[g for g in groups if g['kind']=='exact'];self.assertEqual(len(exact),1);self.assertEqual({v['id'] for v in exact[0]['videos']},{a,b});self.assertEqual(len([g for g in groups if g['kind']=='candidate']),1)
  with self.app.db() as conn:conn.execute('INSERT INTO duplicate_choices VALUES(?,?)',(exact[0]['key'],a))
  self.assertEqual(next(g for g in self.app.duplicate_groups() if g['kind']=='exact')['preferred_id'],a)
  (self.root/'b.mp4').write_bytes(b'54321');self.assertFalse(any(g['kind']=='exact' for g in self.app.duplicate_groups()))
  self.assertTrue((self.root/'a.mp4').exists());self.assertTrue((self.root/'b.mp4').exists())
 def test_auto_queue_dedup_and_limit(self):
  self.add('a.mp4',b'a','ABC-1');self.add('b.mp4',b'b','ABC-1');self.add('c.mp4',b'c','DEF-2');self.add('d.mp4',b'd','GHI-3');calls=[]
  def request(path,data=None):calls.append(data['number']);return {'id':100+len(calls),'status':'queued'}
  self.app.amane_request=request;self.app.auto_scrape_tick();self.assertEqual(len(calls),2);self.assertEqual(len(set(calls)),2)
  self.app.auto_scrape_tick();self.assertEqual(len(calls),3);self.app.auto_scrape_tick();self.assertEqual(len(calls),3)
  cfg=self.app.config();cfg['auto_scrape']=False;(self.app.DATA/'settings.json').write_text(json.dumps(cfg),'utf-8');self.app.auto_scrape_tick();self.assertFalse(self.app.AUTO_STATUS['enabled'])
 def test_parts_and_vr(self):
  app=self.app
  for name,index in [('ABC-123 part1.mp4',1),('ABC-123_PART02.mkv',2),('ABC-123-CD3.mp4',3),('ABC-123 第4部分.mp4',4)]:
   row=app.presentation({'path':str(self.root/name),'height':2160});self.assertEqual(row['part_index'],index);self.assertEqual(row['resolution_label'],'4K')
  a=self.add('ABC-123-part1.mp4',b'111','ABC-123');b=self.add('ABC-123-part2.mp4',b'2222','ABC-123')
  self.assertEqual(app.duplicate_groups(),[])
  self.assertFalse(app.presentation({'path':'VRKM-123.mp4'})['vr'])
  self.assertTrue(app.presentation({'path':'ABC-123 VR180.mp4'})['vr'])
  self.assertTrue(app.presentation({'path':'ABC-123.mp4','amane_snapshot':'{"vr":true}'})['vr'])
  self.add('ABC-123-part1-copy.mp4',b'33333','ABC-123');self.assertTrue(app.duplicate_groups())
 def test_actor_library(self):
  a=self.add('a-part1.mp4',b'a','ABC-1');b=self.add('a-part2.mp4',b'b','ABC-1');c=self.add('other.mp4',b'c','DEF-2')
  with self.app.db() as db:
   db.execute('UPDATE videos SET actors=? WHERE id IN (?,?)',('演员A, 演员B',a,b));db.execute('UPDATE videos SET actors=? WHERE id=?',('演员A;演员C',c));db.execute('INSERT INTO actor_preferences VALUES(?,?,?)',('演员A',5,1))
  people={x['name']:x for x in self.app.actor_library()};self.assertEqual(people['演员A']['movie_count'],2);self.assertEqual(people['演员A']['file_count'],3);self.assertEqual(people['演员A']['rating'],5);self.assertEqual(people['演员A']['favorite'],1);self.assertEqual(people['演员B']['movie_count'],1)
 def test_censorship(self):
  app=self.app
  self.assertEqual(app.censorship({'path':'ABC-123.mp4','amane_snapshot':'{"content_type":"censored"}'})[0],'censored')
  self.assertEqual(app.censorship({'path':'ABC-123 无码.mp4','amane_snapshot':'{"content_type":"censored"}'})[0],'uncensored')
  self.assertEqual(app.censorship({'path':'ABC-123 uncensored.mp4'})[0],'uncensored')
  self.assertEqual(app.censorship({'path':'FC2-123.mp4','amane_snapshot':'{"content_type":"fc2"}'})[0],'unknown')
  self.assertEqual(app.censorship({'path':'有码无码.mp4'})[0],'unknown')
  self.assertEqual(app.censorship({'path':'无码.mp4','censor_override':'censored'})[0],'censored')
  self.assertEqual(app.presentation({'path':'a.mp4','amane_snapshot':'{"content_type":"uncensored"}'})['censorship_label'],'无码')
 def test_failed_cooldown(self):
  self.add('a.mp4',b'a','ABC-1')
  with self.app.db() as c:c.execute('INSERT INTO scrape_jobs(code,status,created) VALUES(?,?,?)',('ABC-1','failed',self.app.time.time()))
  self.app.submit_scrapes=lambda ids:self.fail('must not resubmit during cooldown');self.app.auto_scrape_tick()
if __name__=='__main__':unittest.main()
