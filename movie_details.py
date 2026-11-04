"""Bounded per-work/version/part reads; no hierarchy hydration or media writes."""
from library_query import LibraryQueryChanged
from media_presentation import resolution_label
from pathlib import Path
import uuid


class MovieDetails:
    # Preserve legacy quality/duplicate ordering, including the original first-seen tie.
    ORDER="""b.all_missing,b.preferred DESC,b.min_height DESC,b.total_size DESC,
        b.version_mtime DESC,b.version_first_id,b.version_key"""
    LABELS={'censored':'有码','uncensored':'无码','unknown':'未知'}

    def __init__(self,browse,review):
        self.browse,self.review=browse,review
        self.epoch=uuid.uuid4().hex

    @staticmethod
    def number(query,key,default,minimum=0,maximum=None):
        value=int(str(query.get(key,default)))
        if value<minimum:raise ValueError('分页参数无效：'+key)
        return min(value,maximum) if maximum else value

    def header(self,row):
        height=int(row['min_height'] or 0)
        resolution=resolution_label(height)
        path=Path(row['path']);label=(path.parent.name+' / '+path.name).strip(' /')
        return {'version_key':row['version_key'],'resume_id':row['id'] if row['all_missing'] else row['resume_id'],
                'part_count':row['part_count'],'height':height,'size':row['total_size'],'duration':row['total_duration'],
                'watched':bool(row['all_watched']),'position':row['max_position'],'missing':bool(row['all_missing']),
                'preferred':bool(row['preferred']),'resolution_label':resolution,
                'version_label':label[:120]+' · '+('最低 ' if row['part_count']>1 and height else '')+resolution+' · '+self.LABELS[row['censorship']]+' · '+(row['codec'] or '未知编码')}

    def query(self,query):
        ident=self.number(query,'id',0,1)
        exact=str(query.get('exact','0'))
        if exact not in ('0','1'):raise ValueError('精确文件参数无效')
        voffset=self.number(query,'version_offset',0)
        vlimit=self.number(query,'version_limit',10,1,20)
        plimit=self.number(query,'part_limit',20,1,50)
        self.browse.refresh()
        with self.browse.db() as c:
            c.execute('BEGIN')
            revision=c.execute('SELECT revision FROM browse_state WHERE id=1').fetchone()[0]
            revision=self.epoch+':'+str(revision)
            if 'revision' in query and str(query['revision'])!=str(revision):raise LibraryQueryChanged('作品资料已更新，请重新打开作品后翻页')
            anchor=c.execute('SELECT l.movie_key,l.version_key FROM media_links l JOIN browse_projection p ON p.video_id=l.video_id AND p.visible=1 WHERE l.video_id=?',(ident,)).fetchone()
            if not anchor:raise ValueError('作品不存在或已排除')
            key=anchor['movie_key']
            edit_count=c.execute('SELECT COUNT(*) FROM media_links l JOIN videos v ON v.id=l.video_id WHERE l.movie_key=?',(key,)).fetchone()[0]
            totals=c.execute('SELECT COUNT(*),SUM(part_count),MAX(movie_favorite),MAX(movie_mtime),MAX(CASE WHEN any_missing=0 THEN min_height ELSE 0 END) FROM browse_version_rows WHERE movie_key=?',(key,)).fetchone()
            if not totals[0]:raise ValueError('作品不存在或已排除')
            default=c.execute('SELECT b.* FROM browse_version_rows b WHERE b.movie_key=? ORDER BY b.preference_rank,'+self.ORDER+' LIMIT 1',(key,)).fetchone()
            default_header=self.header(default)
            if exact=='0':ident=default_header['resume_id']
            selected_raw=c.execute('SELECT v.*,l.movie_key,l.version_key,l.part_no,l.manual AS relation_manual FROM videos v JOIN media_links l ON l.video_id=v.id JOIN browse_projection p ON p.video_id=v.id AND p.visible=1 WHERE v.id=? AND l.movie_key=?',(ident,key)).fetchone()
            if not selected_raw:raise LibraryQueryChanged('播放文件已变化，请重新打开作品')
            version=selected_raw['version_key']
            active=self.header(c.execute('SELECT * FROM browse_version_rows WHERE movie_key=? AND version_key=?',(key,version)).fetchone())
            if voffset>=totals[0]:raise ValueError('版本页码超出范围')
            versions=[self.header(r) for r in c.execute('SELECT b.* FROM browse_version_rows b WHERE b.movie_key=? ORDER BY '+self.ORDER+' LIMIT ? OFFSET ?',(key,vlimit,voffset))]
            if 'part_offset' in query:poffset=self.number(query,'part_offset',0)
            else:
                before=c.execute('SELECT COUNT(*) FROM media_links l JOIN browse_projection p ON p.video_id=l.video_id AND p.visible=1 WHERE l.movie_key=? AND l.version_key=? AND (l.part_no<? OR (l.part_no=? AND l.video_id<?))',(key,version,selected_raw['part_no'],selected_raw['part_no'],ident)).fetchone()[0]
                poffset=(before//plimit)*plimit
            if poffset>=active['part_count']:raise ValueError('分段页码超出范围')
            raw_parts=c.execute('SELECT v.*,l.movie_key,l.version_key,l.part_no,l.manual AS relation_manual FROM media_links l JOIN videos v ON v.id=l.video_id JOIN browse_projection p ON p.video_id=v.id AND p.visible=1 WHERE l.movie_key=? AND l.version_key=? ORDER BY l.part_no,v.id LIMIT ? OFFSET ?',(key,version,plimit,poffset)).fetchall()
            def present(raw):
                row=self.browse.core.present(dict(raw));row['part_index']=raw['part_no'];row.pop('part_no',None)
                return row
            parts=[present(r) for r in raw_parts]
            selected=next((r for r in parts if r['id']==ident),None)
            if selected is None:selected=present(selected_raw)
            files=parts if any(p['id']==ident for p in parts) else parts+[selected]
            next_row=c.execute('SELECT v.id,v.missing FROM media_links l JOIN videos v ON v.id=l.video_id JOIN browse_projection p ON p.video_id=v.id AND p.visible=1 WHERE l.movie_key=? AND l.version_key=? AND (l.part_no>? OR (l.part_no=? AND v.id>?)) ORDER BY l.part_no,v.id LIMIT 1',(key,version,selected['part_index'],selected['part_index'],ident)).fetchone()
            # SQL selects a version without indexed missing parts; no media is probed.
            quality_context=[{'_versions':[{'height':active['height'],'_parts':files},{'height':totals[4] or 0,'_parts':[]}]}]
            review=self.review.snapshot(files,quality_context)
            detail={'movie_key':key,'default_version_key':default_header['version_key'],'default_version':default_header,'selected':selected,'active_version':active,
                    'total_files':totals[1],'edit_file_count':edit_count,'favorite':bool(totals[2]),'mtime':totals[3],'revision':revision,
                    'versions':{'items':versions,'offset':voffset,'limit':vlimit,'total':totals[0]},
                    'parts':{'items':parts,'offset':poffset,'limit':plimit,'total':active['part_count']},'next':dict(next_row) if next_row else None}
        return {'detail':detail,'review':review}

    def for_edit(self,ident,maximum):
        maximum=int(maximum)
        if not 1<=maximum<=100:raise ValueError('关系编辑范围必须在1–100个文件内')
        self.browse.refresh()
        with self.browse.db() as c:
            # The connection's application mutex spans this check and legacy hydration.
            anchor=c.execute('SELECT l.movie_key FROM media_links l JOIN browse_projection p ON p.video_id=l.video_id AND p.visible=1 WHERE l.video_id=?',(int(ident),)).fetchone()
            if not anchor:raise ValueError('作品不存在或已排除')
            total=c.execute('SELECT COUNT(*) FROM media_links l JOIN videos v ON v.id=l.video_id WHERE l.movie_key=?',(anchor[0],)).fetchone()[0]
            if total>maximum:raise ValueError(f'该作品包含超过{maximum}个索引文件，请仅编辑当前文件或缩小范围')
            works=self.browse.core.hierarchy(movie_key=anchor[0])
            if not works:raise ValueError('作品不存在或已排除')
            return works[0]
