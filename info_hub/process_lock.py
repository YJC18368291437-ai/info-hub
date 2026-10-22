"""OS-backed process lock; stale files do not mean stale locks."""
import os
import sys
from pathlib import Path


class ProcessLock:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.file = path.open('a+b')

    def acquire(self):
        self.file.seek(0)
        self.file.write(b'0'); self.file.flush(); self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('info-hub instance already running') from None
        return self

    def close(self):
        self.file.close()


def process_image():
    if os.name == 'nt':
        import ctypes
        buf=ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetModuleFileNameW(None,buf,len(buf)):
            raise OSError('Cannot determine own process image')
        return buf.value
    return sys.executable
