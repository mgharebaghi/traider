"""OS-held lock preventing two bots from managing the same data directory."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def instance_lock(data_dir: Path):
    handle = (data_dir / "bot.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Another bot is already using this DATA_DIR") from None
    try:
        yield
    finally:
        handle.close()  # Closing releases the OS lock, including on process exit.
