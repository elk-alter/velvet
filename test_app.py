import os,tempfile,unittest,importlib.util,threading,json
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError

class LibraryTests(unittest.TestCase):
 def test_workflow(self):
  with tempfile.TemporaryDirectory() as tmp:
   os.environ['VELVET_DATA']=str(Path(tmp)/'data')
   spec=importlib.util.spec_from_file_location('velvet',Path(__file__).with_name('app.py'));app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
   app.binary=lambda name:None
   media=Path(tmp)/'media';media.mkdir();video=media/'ABC-123.mp4';video.write_bytes(b'0123456789')
   video.with_suffix('.nfo').write_text('<movie><title>Fixture</title><actor><name>Actor</name></actor><tag>Test</tag></movie>')
   with app.db() as c:c.execute('INSERT INTO folders VALUES(?)',(str(media),))
   app.scan()
   page=app.BROWSE.query({'limit':1})
   app.scan()
   repeated=app.BROWSE.query({'limit':1,'snapshot':page['snapshot']})
   self.assertEqual(repeated['revision'],page['revision'])
   self.assertEqual(repeated['snapshot'],page['snapshot'])
   with app.db() as c:
    row=c.execute('SELECT * FROM videos').fetchone();vid=row['id'];self.assertEqual(row['title'],'Fixture');self.assertEqual(row['actors'],'Actor');self.assertEqual(row['code'],'ABC-123')
    c.execute('UPDATE videos SET title=?,manual=1 WHERE id=?',('Manual',vid))
   tags,reasons=app.classify({'path':str(media/'[精选] ABC-123.mp4'),'height':2160,'duration':7200,'codec':'hevc','source':'nfo','code':'ABC-123'})
   self.assertIn('画质/4K',tags);self.assertIn('时长/长片',tags);self.assertIn('文件标签/精选',tags);self.assertIn('资料/已匹配',tags)
   (app.DATA/'tag_rules.json').write_text(json.dumps([{'tag':'自定义精选','keywords':['ABC-123']}]),encoding='utf-8')
   app.retag_all()
   with app.db() as c:
    r=c.execute('SELECT * FROM videos').fetchone();self.assertIn('自定义精选',r['auto_tags']);self.assertEqual(r['tags'],'Test')
   original_request=app.amane_request
   app.amane_request=lambda path:{'total':1,'items':[{'id':42,'number':'ABC-123','title':'Matched title','field_sources':{'title':'fixture'},'tags':['分类A','分类A'],'actors':['Actor']}]}
   self.assertEqual(app.sync_amane(),1)
   with app.db() as c:
    r=c.execute('SELECT * FROM videos').fetchone();self.assertEqual(r['title'],'Manual');self.assertEqual(r['source_tags'],'分类A');self.assertIn('题材/分类A',r['auto_tags'])
   app.amane_request=original_request
   app.scan(True)
   with app.db() as c:self.assertEqual(c.execute('SELECT title FROM videos').fetchone()[0],'Manual')
   video.unlink();app.scan()
   with app.db() as c:self.assertEqual(c.execute('SELECT missing FROM videos').fetchone()[0],1)
   video.write_bytes(b'0123456789');app.scan()
   with app.db() as c:self.assertEqual(c.execute('SELECT missing FROM videos').fetchone()[0],0)
   server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   base='http://127.0.0.1:'+str(server.server_port)
   try:
    def req(path,data=None,headers=None):
     h={'Host':'127.0.0.1:8765'};h.update(headers or {});return urlopen(Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers=h))
    with req('/api/library') as r:self.assertEqual(len(json.load(r)['videos']),1)
    for path,(filename,kind) in app.UI_ASSETS.items():
     with self.subTest(asset=path),req(path) as r:
      self.assertEqual(r.headers.get_content_type(),kind)
      self.assertEqual(r.read(),(app.ROOT/filename).read_bytes())
    with req('/api/actor-edit',{'name':'Actor','rating':4,'favorite':1},{'X-Velvet-Token':app.TOKEN}) as r:self.assertTrue(json.load(r)['ok'])
    with req('/api/actors') as r:
     actor=next(x for x in json.load(r)['actors'] if x['name']=='Actor');self.assertEqual(actor['rating'],4);self.assertEqual(actor['favorite'],1)
    with req('/api/actor-edit',{'name':'Actor','favorite':0},{'X-Velvet-Token':app.TOKEN}) as r:self.assertTrue(json.load(r)['ok'])
    with req('/api/actors') as r:self.assertEqual(next(x for x in json.load(r)['actors'] if x['name']=='Actor')['rating'],4)
    with req('/api/file-review/mark',{'ids':[vid],'flag':'watermark','note':'fixture'},{'X-Velvet-Token':app.TOKEN}) as r:self.assertTrue(json.load(r)['ok'])
    with req('/api/file-review') as r:self.assertTrue(json.load(r)['files'][str(vid)]['needs_update'])
    with req('/api/file-review/mark',{'ids':[vid],'decision':'accepted'},{'X-Velvet-Token':app.TOKEN}) as r:self.assertTrue(json.load(r)['ok'])
    with req('/api/file-review') as r:self.assertFalse(json.load(r)['files'][str(vid)]['needs_update'])
    with self.assertRaises(HTTPError):req('/api/file-review/mark',{'ids':[vid],'flag':'damaged'})

    with self.assertRaises(HTTPError):req('/api/actor-edit',{'name':'Actor','rating':6},{'X-Velvet-Token':app.TOKEN})

    with req('/media/'+str(vid),headers={'Range':'bytes=2-5'}) as r:self.assertEqual(r.status,206);self.assertEqual(r.read(),b'2345')
    with req('/media/'+str(vid),headers={'Range':'bytes=-3'}) as r:self.assertEqual(r.read(),b'789')
    with self.assertRaises(HTTPError) as error:req('/api/edit',{'id':vid,'title':'blocked'})
    self.assertEqual(error.exception.code,403)
    with req('/api/edit',{'id':vid,'title':'Updated'}, {'X-Velvet-Token':app.TOKEN}) as r:self.assertTrue(json.load(r)['ok'])
    with app.db() as c:c.execute('UPDATE videos SET manual=0,locked_fields=? WHERE id=?',('[]',vid))
    with req('/api/edit',{'id':vid,'title':'Locked title','favorite':1},{'X-Velvet-Token':app.TOKEN}) as r:self.assertTrue(json.load(r)['ok'])
    old_request=app.amane_request
    app.amane_request=lambda path,data=None:({'id':99,'status':'queued'} if data else {'status':'failed','error':'fixture failure','result':{'outcomes':[{'site':'test','outcome':'failed','reason':'network'}]}} if path.startswith('tasks/') else {'total':1,'items':[{'id':42,'number':'ABC-123','title':'Remote','actors':['New Actor'],'studio':'New Studio','tags':['Topic'],'field_sources':{'title':'fixture'}}]})
    app.sync_amane()
    with app.db() as c:
     row=c.execute('SELECT * FROM videos WHERE id=?',(vid,)).fetchone();self.assertEqual(row['title'],'Locked title');self.assertEqual(row['studio'],'New Studio');self.assertEqual(row['favorite'],1);self.assertEqual(json.loads(row['locked_fields']),['title'])
    self.assertEqual(app.submit_scrapes([vid])['submitted'],1)
    self.assertEqual(app.submit_scrapes([vid])['skipped'],1)
    app.poll_scrapes()
    with app.db() as c:
     job=c.execute('SELECT * FROM scrape_jobs').fetchone();self.assertEqual(job['status'],'failed');self.assertIn('network',job['details'])
    app.amane_request=old_request
    with req('/api/library') as r:
     settings=json.load(r)['settings'];self.assertEqual(settings['amane_token'],'')
    with req('/api/edit',{'id':vid,'code':'XYZ-999'},{'X-Velvet-Token':app.TOKEN}) as r:self.assertTrue(json.load(r)['ok'])
    with app.db() as c:
     row=c.execute('SELECT * FROM videos WHERE id=?',(vid,)).fetchone();self.assertEqual(row['amane_id'],0);self.assertEqual(row['source_tags'],'')

   finally:server.shutdown();server.server_close();thread.join()
if __name__=='__main__':unittest.main()
