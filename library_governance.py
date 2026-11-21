"""Reversible, local catalog rules and actor identities."""
import hashlib
import json
import re
from media_presentation import category_tags


def record(connection, name, default):
    row=connection.execute('SELECT value FROM app_state WHERE name=?',(name,)).fetchone()
    return json.loads(row[0]) if row else default


def save_record(connection, name, value):
    connection.execute('INSERT INTO app_state VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                       (name,json.dumps(value,ensure_ascii=False,sort_keys=True)))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def names(value):
    return [name.strip() for name in re.split(r'[,，;；\n]',value or '') if name.strip()]


def display_tags(row,aliases):
    manual=set(names(row.get('tags')))
    return list(dict.fromkeys(tag if tag in manual else aliases.get(tag,tag)
                              for tag in names(category_tags(row))))


def actor_state(connection):
    return record(connection,'actor_identity',{'aliases':{},'assignments':{}})


def actor_names(state, video_id, raw):
    aliases=state['aliases']
    assignments=state['assignments'].get(str(video_id),{})
    return list(dict.fromkeys(assignments.get(name,aliases.get(name,name)) for name in names(raw)))


class CatalogGovernance:
    def __init__(self,db,legacy,visible,classify,portrait=lambda name:None):
        self.db,self.legacy,self.visible,self.classify=db,legacy,visible,classify
        self.portrait=portrait

    def tag_config(self,connection=None):
        if connection is None:
            with self.db() as c:return self.tag_config(c)
        current=record(connection,'tag_governance',None)
        return current if current is not None else self.legacy()

    @staticmethod
    def normalize_tags(aliases,rules):
        if not isinstance(aliases,dict) or len(aliases)>500 or not isinstance(rules,list) or len(rules)>100:
            raise ValueError('最多 500 条别名、100 条关键词规则')
        def label(value):
            if not isinstance(value,str) or not value.strip() or len(value.strip())>80 or re.search(r'[,，;；\n]',value):
                raise ValueError('标签或别名须为 1–80 字，使用单个标签名称')
            return value.strip()
        mapped={}
        for raw,target in aliases.items():
            source=label(raw);target=label(target)
            if source in mapped:raise ValueError('同一个来源标签只能指定一个别名')
            mapped[source]=target
        if any(target in mapped for target in mapped.values()):
            raise ValueError('别名目标不能再次作为来源，请将所有写法直接指向最终名称')
        normalized=[]
        for rule in rules:
            words=rule.get('keywords') if isinstance(rule,dict) else None
            if not isinstance(words,list) or not 1<=len(words)<=50:
                raise ValueError('每条规则须有 1–50 个关键词')
            normalized.append({'tag':label(rule.get('tag')),'keywords':list(dict.fromkeys(label(word) for word in words))})
        return {'aliases':mapped,'rules':normalized}

    def inspect_tags(self,connection,candidate):
        previous=self.tag_config(connection)
        rows=self.visible([dict(row) for row in connection.execute('SELECT * FROM videos ORDER BY id')])
        changes=[];samples=[]
        for row in rows:
            tags,reasons=self.classify(row,candidate)
            before=set(display_tags(row,previous['aliases']))
            after=set(display_tags(row,candidate['aliases']))
            if tags!=(row['auto_tags'] or '') or before!=after:
                changes.append((tags,reasons,row['id']))
                if len(samples)<10:
                    old=set(names(row['auto_tags']))|before
                    new=set(names(tags))|after
                    samples.append({'title':row['title'],'added':sorted(new-old),'removed':sorted(old-new)})
        summary={'config':candidate,'changed':len(changes),'samples':samples,
                 'digest':fingerprint([previous,candidate,changes,samples])}
        return summary,changes

    def tags(self,payload,action):
        with self.db() as c:
            if action in ('apply','undo'):c.execute('BEGIN IMMEDIATE')
            old=self.tag_config(c)
            if action in ('undo','undo-preview'):
                prior=record(c,'tag_governance_undo',None)
                if not prior or fingerprint(old)!=prior['after']:raise ValueError('没有可以撤销的标签规则变更')
                candidate=prior['before']
            else:
                candidate=self.normalize_tags(payload.get('aliases'),payload.get('rules'))
            summary,changes=self.inspect_tags(c,candidate)
            if action in ('preview','undo-preview'):return summary
            if payload.get('digest')!=summary['digest']:raise ValueError('规则或片库已变化，请重新预览')
            save_record(c,'tag_governance',candidate)
            save_record(c,'tag_governance_undo',None if action=='undo' else {'before':old,'after':fingerprint(candidate)})
            c.executemany('UPDATE videos SET auto_tags=?,tag_reasons=? WHERE id=?',changes)
            c.execute('UPDATE browse_state SET rebuild=1,revision=revision+1 WHERE id=1')
            return summary

    def actor_preview(self,connection,payload):
        source=payload.get('source');target=payload.get('target');mode=payload.get('mode')
        if mode not in ('merge','split') or not all(isinstance(x,str) and 0<len(x.strip())<=200 and names(x.strip())==[x.strip()] for x in (source,target)):
            raise ValueError('请填写有效的演员名称和操作')
        source,target=source.strip(),target.strip()
        if source==target:raise ValueError('请为不同身份指定不同的显示名称')
        state=actor_state(connection)
        aliases=state['aliases']
        if source in aliases or target in aliases or source in aliases.values():
            raise ValueError('当前身份已有别名关系，请先撤销原操作或选用主名称')
        rows=self.visible([dict(row) for row in connection.execute('SELECT id,path,size,title,actors FROM videos WHERE missing=0 ORDER BY id')])
        source_rows=[row for row in rows if source in actor_names(state,row['id'],row['actors'])]
        target_rows=[row for row in rows if target in actor_names(state,row['id'],row['actors'])]
        if not source_rows:raise ValueError('来源演员没有可用作品')
        ids=payload.get('ids',[])
        if mode=='split':
            if any(source in assignments.values() for assignments in state['assignments'].values()):
                raise ValueError('这是已拆分的身份，请选择原始名称进行拆分')
            if target_rows or target in aliases.values():raise ValueError('同名拆分请使用一个新的显示名')
            if not isinstance(ids,list) or len(ids)>100 or any(type(x) is not int for x in ids) or len(set(ids))!=len(ids):
                raise ValueError('每次拆分最多选择 100 个文件')
            selected=[row for row in source_rows if row['id'] in ids]
            if len(selected)!=len(ids) or len(selected)==len(source_rows):
                raise ValueError('请选择来源演员的部分作品')
        else:
            if not target_rows:raise ValueError('请选择已有演员作为合并目标')
            selected=source_rows
        profiles={}
        for name,files in ((source,source_rows),(target,target_rows)):
            preference=connection.execute('SELECT rating,favorite FROM actor_preferences WHERE name=?',(name,)).fetchone()
            profiles[name]={'files':len(files),'rating':preference[0] if preference else 0,
                            'favorite':bool(preference and preference[1]),'saved':bool(preference),
                            'portrait':self.portrait(name)}
        offset=payload.get('offset',0)
        if type(offset) is not int or offset<0:raise ValueError('文件页码无效')
        offset=min(offset,max(0,(len(source_rows)-1)//25*25))
        digest=fingerprint([state,source,target,mode,profiles,[row['id'] for row in selected],
                            [row['id'] for row in target_rows]])
        samples=[{'id':row['id'],'title':row['title']} for row in source_rows[offset:offset+25]]
        return {'source':source,'target':target,'mode':mode,'profiles':profiles,'samples':samples,
                'selected':len(selected),'offset':offset,'total':len(source_rows),'digest':digest},state,selected

    def actors(self,payload,action):
        with self.db() as c:
            if action!='preview':c.execute('BEGIN IMMEDIATE')
            if action=='undo':
                state=actor_state(c);previous=record(c,'actor_identity_undo',None)
                if not previous or fingerprint(state)!=previous['after']:raise ValueError('没有可撤销的演员变更')
                target=previous['target']
                current=c.execute('SELECT rating,favorite FROM actor_preferences WHERE name=?',(target,)).fetchone()
                if not current or list(current)!=previous['saved']:raise ValueError('目标偏好已修改，请先核对后再撤销')
                if previous['preference'] is None:c.execute('DELETE FROM actor_preferences WHERE name=?',(target,))
                else:c.execute('UPDATE actor_preferences SET rating=?,favorite=? WHERE name=?',(*previous['preference'],target))
                save_record(c,'actor_identity',previous['before'])
                save_record(c,'actor_identity_undo',None)
                result={'target':target,'source':previous['source']}
            else:
                result,state,selected=self.actor_preview(c,payload)
                if action=='preview':return result
                if payload.get('digest')!=result['digest'] or not selected:raise ValueError('选择或演员资料已变化，请重新预览')
                rating=payload.get('rating',0);favorite=payload.get('favorite',False)
                if type(rating) is not int or rating not in range(6) or type(favorite) is not bool:
                    raise ValueError('请选择有效的评分与收藏状态')
                before=json.loads(json.dumps(state));source,target=result['source'],result['target']
                if result['mode']=='merge':
                    state['aliases'][source]=target
                    for assignments in state['assignments'].values():
                        for raw,name in assignments.items():
                            if name==source:assignments[raw]=target
                else:
                    for row in selected:state['assignments'].setdefault(str(row['id']),{})[source]=target
                old=result['profiles'][target]
                preference=[old['rating'],int(old['favorite'])] if old['saved'] else None
                c.execute('INSERT INTO actor_preferences VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET rating=excluded.rating,favorite=excluded.favorite',
                          (target,rating,int(favorite)))
                save_record(c,'actor_identity',state)
                save_record(c,'actor_identity_undo',{'before':before,'after':fingerprint(state),'source':source,
                                                     'target':target,'preference':preference,'saved':[rating,int(favorite)]})
            c.execute('UPDATE browse_state SET rebuild=1,revision=revision+1 WHERE id=1')
            return result
