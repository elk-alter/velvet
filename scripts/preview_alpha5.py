"""Disposable synthetic UI fixture. Does not open the user data directory or remote sources."""
import tempfile,os,sys,json,time,argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--port",type=int,default=18765);parser.add_argument("--count",type=int,default=72);args=parser.parse_args()
os.environ["VELVET_PORT"]=str(args.port)
with tempfile.TemporaryDirectory() as tmp:
 os.environ["VELVET_DATA"]=str(Path(tmp)/"data")
 tmp=str(Path(tmp).resolve())
 media=Path(tmp)/"Synthetic-Media";media.mkdir()
 import app
 cfg=app.config();cfg.update(scrape_engine="builtin",auto_scrape=False);(app.DATA/"settings.json").write_text(json.dumps(cfg))
 names=["Alex Morgan","Emma Reed","Sofia Lin","Noah Chen","Mia Park","Leo Brooks"]
 titles=["The Quiet Hours","Beyond the Valley","Blue Afternoon","Northern Lights","A Place to Return","The Last Summer","Between Two Cities","Somewhere in Time"]
 with app.db() as c:
  for i in range(1,args.count+1):
   title=titles[(i-1)%len(titles)]+f" {i:02d}"
   c.execute("INSERT INTO videos(id,path,title,size,mtime,code,actors,source_tags,studio,height,width,duration,source,amane_id,amane_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(i,str(media/("Example Collection" if i<=3 else "Film "+str(i))/("film"+str(i)+".mp4")),title,1500000000,time.time()-i,"FILM-"+str(i).zfill(3),names[(i-1)%6],"Drama,Travel" if i%2 else "Documentary,Art","Studio North" if i%2 else "Field Notes",2160 if i%3==0 else 1080,3840 if i%3==0 else 1920,3600+i*120,"amane",i,json.dumps({"series":"Stories","directors":["Director One"]})))
  for name in names:
   key=app.actor_key(name);(app.DATA/"actors"/(key+".jpg")).write_bytes(b"synthetic")
   c.execute("INSERT INTO actor_profiles(name,profile,origin,updated) VALUES(?,?,?,?)",(name,"{}","Demo",time.time()))
 with app.db() as c:
  c.execute("INSERT INTO folders(path) VALUES(?)",(str(media),))
  for row in (c.execute("SELECT path FROM videos") if args.count<=1000 else []):
   file=Path(row[0]);file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(b"synthetic preview fixture; not playable")
 app.PEOPLE.start=lambda *args,**kwargs:False
 class Preview(app.Handler):
  def do_GET(self):
   if self.path.startswith('/api/video-artwork/'):
    vid=int(self.path.split('/')[-1]);return self.send({'items':[{'url':f'/thumb/{vid}-still-{i}.jpg','kind':'still','label':'Still '+str(i)} for i in range(1,5)]+[{'url':f'/thumb/{vid}-{i}.jpg','kind':'screenshot','label':'Local screenshot '+str(i)} for i in range(1,3)],'stills':4,'screenshots':2,'pending':0,'failed':0})
   if self.path.startswith(("/thumb/","/actor-image/")):
    number=sum(self.path.encode())%6;colors=["#687c83","#6c695f","#8a7162","#526778","#878674","#707b70"];color=colors[number]
    actor=self.path.startswith("/actor-image/")
    art=("<circle cx=\"200\" cy=\"175\" r=\"70\" fill=\"#ffffff44\"/><path d=\"M60 510Q70 285 200 285Q330 285 340 510\" fill=\"#ffffff44\"/>" if actor else "<circle cx=\"280\" cy=\"190\" r=\"125\" fill=\"#ffffff14\"/><path d=\"M-30 500Q120 180 430 400V650H-30Z\" fill=\"#ffffff18\"/><text x=\"38\" y=\"505\" fill=\"#ffffffcc\" font-size=\"38\" font-family=\"serif\">STORIES</text><text x=\"40\" y=\"540\" fill=\"#ffffff88\" font-size=\"13\" letter-spacing=\"5\">VELVET COLLECTION</text>")
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600" viewBox="0 0 400 600"><rect width="400" height="600" fill="{color}"/>{art}</svg>'
    return self.send(svg.encode(),"image/svg+xml")
   super().do_GET()
 server=app.ThreadingHTTPServer(("127.0.0.1",args.port),Preview);print("synthetic browse preview ready",flush=True)
 try:server.serve_forever()
 except KeyboardInterrupt:server.server_close()
