"""Opt-in lossless wire references for repeated movie/version/part metadata."""
def pack_library(payload):
    files={row['id']:row for row in payload['videos']}
    def encode(value):
        if isinstance(value,list):return [encode(item) for item in value]
        if not isinstance(value,dict):return value
        base=files.get(value.get('id')) if 'path' in value else None
        if base and value.get('path')==base['path']:
            return {'$video':value['id'],'$delta':{k:encode(v) for k,v in value.items() if k not in base or v!=base[k]}}
        return {k:encode(v) for k,v in value.items()}
    return dict(payload,movies=encode(payload['movies']),transport='video-references-v1')
