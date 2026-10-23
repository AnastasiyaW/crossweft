"""State signatures shared by the tracer (in a validator's process) and the runner.

Both sides must compute them identically, so they live in one file the tracer imports
from its own directory and crossweft/vcache.py loads by path.
"""
import os


def path_state(path, real_stat=os.stat, real_lstat=os.lstat):
    """What a stat/exists/getsize/getmtime caller can learn about a path."""
    try:
        st = real_stat(path)
    except OSError:
        try:
            real_lstat(path)
            return "other"          # a broken link
        except OSError:
            return "missing"
    import stat as _stat
    if _stat.S_ISDIR(st.st_mode):
        return "dir"
    if _stat.S_ISREG(st.st_mode):
        return f"file:{st.st_size}:{st.st_mtime_ns}"
    return "other"


def listing_state(path, real_scandir=os.scandir):
    """The names in a directory and the kind of each (sizes and times of entries a
    caller stats through a DirEntry are recorded as path states of their own)."""
    import hashlib
    try:
        with real_scandir(path) as entries:
            names = sorted((e.name + "/") if e.is_dir() else e.name for e in entries)
    except OSError:
        return None
    return hashlib.sha256("\0".join(names).encode("utf-8", "surrogateescape")).hexdigest()
