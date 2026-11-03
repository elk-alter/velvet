"""Cross-process exclusive ownership of one data directory, including offline restore."""
import hashlib
import os
from pathlib import Path
from functools import wraps


class DataGuard:
    def __init__(self, data):
        data=Path(data).resolve();data.parent.mkdir(parents=True,exist_ok=True)
        key=hashlib.sha256(os.path.normcase(str(data)).encode()).hexdigest()[:24]
        self.file=(data.parent/('.velvet-lock-'+key)).open('a+b')
        try:
            self.file.seek(0);self.file.write(b'0');self.file.flush();self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close();raise ValueError('Data directory is owned by another service or backup/restore operation') from None

    def close(self):
        if not self.file.closed:self.file.close()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


def exclusive_data(function):
    @wraps(function)
    def wrapped(data,*args,**kwargs):
        with DataGuard(data):return function(data,*args,**kwargs)
    return wrapped
