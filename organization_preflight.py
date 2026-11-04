"""Read-only destination, access and capacity checks before approved file operations."""
import ctypes
import os
import re
import shutil
import stat
import unicodedata
from pathlib import Path


LABELS={'planned_collision':'多个文件的目标名称冲突','long_path':'目标路径超过当前240字符限制','invalid_component':'目标含保留名称或不兼容字符','target_exists':'目标或同名变体已存在','linked_path':'路径包含链接或挂载绕行','parent_alias_collision':'父目录存在大小写或Unicode名称变体','target_parent_not_directory':'目标父路径被文件占用','target_permission_denied':'没有目标目录的创建权限','target_unavailable':'目标路径暂不可用','target_read_only':'目标卷只读','source_permission_denied':'没有源文件的移动权限','fat32_file_limit':'文件超过FAT32单文件限制','preflight_unavailable':'无法完成文件或设备预检','insufficient_space':'目标可用空间不足（含预留）','space_unavailable':'无法读取目标空间','temporary_path_too_long':'任务暂存名会超出路径长度限制，请缩短目录','source_read_denied':'源文件不可读或被其它程序独占','source_changed':'源文件已变化，请重新盘点','device_mismatch':'源设备身份变化，请重新连接核对','source_unavailable':'源文件暂不可用'}


class PreflightError(ValueError):
    def __init__(self,codes):
        self.codes=list(dict.fromkeys(codes))
        super().__init__('整理预检未通过：'+'、'.join(LABELS.get(code,code) for code in self.codes))


def no_links(raw):
    path=Path(raw).expanduser().absolute()
    for part in (path,*path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(),'st_file_attributes',0)&1024):
            raise ValueError('Linked paths require separate review')
    return path.resolve()



def probe_volume(path):
    from storage_inventory import volume_details
    return volume_details(path)


def path_key(path):return unicodedata.normalize('NFC',str(path)).casefold()
def owned_path(path):
    path=Path(os.path.abspath(path))
    # Preserve leaf spelling: a distinct Unicode alias or hard link is not owned.
    return os.path.normcase(str(path.parent))+'/'+path.name


def windows_access(path,rights):
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE;kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW(str(path),rights,7,None,3,0x02000000,None)
    if handle==ctypes.c_void_p(-1).value:return False
    kernel.CloseHandle(handle);return True


def directory_access(path,create_directories=False):
    """Open with create rights; never create a permission probe file."""
    if os.name!='nt':return os.access(path,os.W_OK|os.X_OK)
    return windows_access(path,0x2|(0x4 if create_directories else 0))


def delete_access(path):
    if os.name!='nt':return os.access(Path(path).parent,os.W_OK|os.X_OK)
    return windows_access(path,0x10000)


def read_access(path):
    if os.name!='nt':return os.access(path,os.R_OK)
    return windows_access(path,0x1)


def path_units(path):return len(str(path).encode('utf-16-le'))//2


def valid_component(value,archive=False):
    from archive_policy import ARCHIVE_NAME
    if not value or value in ('.','..') or value.rstrip(' .')!=value:return False
    if value==ARCHIVE_NAME and not archive:return False
    if re.search(r'[<>:"/\\|?*\x00-\x1f]',value):return False
    if re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',value):return False
    return path_units(value)<=255 if os.name=='nt' else len(value.encode('utf-8'))<=255



def nearest_directory(path):
    path=Path(path)
    while not path.exists() and path!=path.parent:path=path.parent
    if not path.is_dir():raise PreflightError(['target_parent_not_directory'])
    return path


def path_conflicts(path,destinations,allowed=(),cache=None,archive=False):
    path=Path(path).absolute();codes=[];key=path_key(path)
    if key in destinations:codes.append('planned_collision')
    destinations.add(key)
    if path_units(path)>240:codes.append('long_path')
    if any(not valid_component(part,archive) for part in path.parts[1:]):codes.append('invalid_component')
    cache=cache if cache is not None else {}
    names_cache=cache.setdefault('names',{});node_cache=cache.setdefault('nodes',{});access_cache=cache.setdefault('access',{})
    if isinstance(allowed,set):allowed_paths=allowed
    else:allowed_paths={owned_path(item) for item in allowed}
    def node(item):
        token=str(item)
        if token not in node_cache:
            try:
                info=item.lstat();node_cache[token]=(True,stat.S_ISDIR(info.st_mode),stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&1024))
            except (FileNotFoundError,NotADirectoryError):node_cache[token]=(False,False,False)
        return node_cache[token]
    def names(parent):
        token=str(parent)
        if token not in names_cache:
            result={}
            with os.scandir(parent) as entries:
                for entry in entries:result.setdefault(path_key(entry.name),[]).append(entry.name)
            names_cache[token]=result
        return names_cache[token]
    try:
        for part in (path,*path.parents):
            if node(part)[2]:codes.append('linked_path');break
        parent=path.parent
        while not node(parent)[0] and parent!=parent.parent:parent=parent.parent
        if not node(parent)[1]:raise PreflightError(['target_parent_not_directory'])
        # Existing directory aliases matter too, including ones created since approval.
        for current in (path.parent,*path.parent.parents):
            if current==current.parent or not node(current.parent)[1]:continue
            for name in names(current.parent).get(path_key(current.name),[]):
                candidate=current.parent/name
                if not node(current)[0] or (candidate.name!=current.name and not os.path.samefile(candidate,current)):codes.append('parent_alias_collision')
        if node(path.parent)[1]:
            for name in names(path.parent).get(path_key(path.name),[]):
                if owned_path(path.parent/name) not in allowed_paths:codes.append('target_exists')
        access_key=(str(parent),parent!=path.parent)
        if access_key not in access_cache:access_cache[access_key]=directory_access(parent,parent!=path.parent)
        if not access_cache[access_key]:codes.append('target_permission_denied')
    except PermissionError:codes.append('target_permission_denied')
    except PreflightError as error:codes.extend(error.codes)
    except OSError:codes.append('target_unavailable')
    return list(dict.fromkeys(codes))


def validate_manifest(entries,device_identity):
    """Validate selected media and companions together; do not register or write."""
    if not entries:return {'version':1,'targets':[]}
    operations={entry['operation'] for entry in entries}
    if operations=={'keep'}:
        if any(not read_access(entry['source']) for entry in entries):raise PreflightError(['source_read_denied'])
        return {'version':1,'files_unchanged':True,'targets':[]}
    moving=operations<={'move','archive'}
    allowed={owned_path(entry['source']) for entry in entries} if moving else set()
    destinations=set();cache={};groups={};codes=[];volumes={};parents={}
    for entry in entries:
        path=Path(entry['destination']);problems=path_conflicts(path,destinations,allowed,cache,archive=entry['operation']=='archive')
        codes.extend(problems)
        if not read_access(entry['source']):codes.append('source_read_denied')
        temporary_parent=Path(entry['source']).parent if moving else path.parent
        temporary_name='.velvet-move-'+'0'*32+'-'+'0'*32+'.partial' if moving else '.velvet-'+'0'*32+'-'+'0'*32+'.partial'
        if path_units(temporary_parent/temporary_name)>240:codes.append('temporary_path_too_long')
        try:
            parent_key=str(path.parent)
            if parent_key not in parents:parents[parent_key]=nearest_directory(path.parent)
            parent=parents[parent_key]
            if str(parent) not in volumes:volumes[str(parent)]=(probe_volume(parent),device_identity(parent))
            volume,identity=volumes[str(parent)]
            if volume.get('read_only'):codes.append('target_read_only')
            size=entry.get('fingerprint',{}).get('size')
            if size is None:size=Path(entry['source']).stat().st_size
            if volume.get('filesystem','').upper()=='FAT32' and size>4294967295:codes.append('fat32_file_limit')
            group=groups.setdefault(identity,{'device':identity,'parent':parent,'bytes':0,'largest':0,'filesystem':volume.get('filesystem','unknown')})
            group['bytes']+=size;group['largest']=max(group['largest'],size)
            if moving and not delete_access(entry['source']):codes.append('source_permission_denied')
        except PermissionError:codes.append('target_permission_denied')
        except PreflightError as error:codes.extend(error.codes)
        except OSError:codes.append('preflight_unavailable')
    targets=[]
    for group in groups.values():
        required=0 if moving else group['bytes'];reserve=16*1024*1024 if moving else max(100*1024*1024,group['largest'],int(required*.05))
        try:
            if shutil.disk_usage(group['parent']).free<required+reserve:codes.append('insufficient_space')
        except OSError:codes.append('space_unavailable')
        targets.append({key:group[key] for key in ('device','filesystem')}|{'required':required,'reserve':reserve})
    if codes:raise PreflightError(codes)
    return {'version':1,'targets':targets}
