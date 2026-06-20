"""
Minimal, dependency-free inotify directory watcher for asyncio (Linux).

Lets a service block until a file event happens in a watched directory and wake
within ~1 ms, instead of polling on a fixed timer. We use it to make the local
QR-trigger hand-off between services instant while keeping the existing
file-drop contract unchanged: the writer (qr.py / the MQTT receiver) still just
drops a file, and the reader is simply *told* about it instead of polling.

Pure ctypes + stdlib on purpose: a freshly cloned SD card needs no extra
`pip install` for the services to run. If inotify is unavailable for any reason
the caller is expected to fall back to its previous polling behaviour, so this
can never make a device worse than before.
"""
import asyncio
import ctypes
import os

# inotify event mask bits (from <sys/inotify.h>)
IN_CLOSE_WRITE = 0x00000008   # a file opened for writing was closed (file drop)
IN_MOVED_TO = 0x00000080      # a file was atomically moved/renamed into the dir
IN_CREATE = 0x00000100        # a file was created in the dir

# The trigger files are produced either by a plain write+close (qr.py,
# record.txt) or by an atomic rename (os.replace in the MQTT receiver), so we
# watch all three. The payload is never inspected — any event just means
# "something landed, rescan now".
DEFAULT_MASK = IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.inotify_init1.argtypes = [ctypes.c_int]
_libc.inotify_init1.restype = ctypes.c_int
_libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
_libc.inotify_add_watch.restype = ctypes.c_int


def _errno_error(prefix):
    err = ctypes.get_errno()
    return OSError(err, f"{prefix}: {os.strerror(err)}")


class DirectoryWatcher:
    """
    Watches a single directory and wakes an asyncio task on the first matching
    event.

    The watch is armed once and kept armed across wait() calls, so an event that
    arrives *between* a scan and the next wait() is still captured (the kernel
    queues it and our reader sets the asyncio.Event) — there is no lost-event
    race with a concurrent directory scan.

    Always pair wait() with a bounded timeout: on timeout the caller just
    rescans the directory exactly like the old polling loop did, only far less
    often. That keeps a missed or filtered event from ever wedging the loop.
    """

    def __init__(self, path, mask=DEFAULT_MASK):
        self._fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._fd < 0:
            raise _errno_error("inotify_init1")
        wd = _libc.inotify_add_watch(self._fd, os.fsencode(path), mask)
        if wd < 0:
            err = _errno_error(f"inotify_add_watch({path})")
            os.close(self._fd)
            self._fd = -1
            raise err
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        self._loop.add_reader(self._fd, self._on_readable)

    def _on_readable(self):
        # Drain everything the kernel has queued. Contents are irrelevant; we
        # only care that *an* event happened so the caller rescans.
        try:
            while True:
                if not os.read(self._fd, 4096):
                    break
        except BlockingIOError:
            pass
        except OSError:
            pass
        self._event.set()

    async def wait(self, timeout):
        """Block until an event arrives or `timeout` seconds elapse.

        Returns True if woken by an event, False on timeout.
        """
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._event.clear()

    def close(self):
        if self._fd is not None and self._fd >= 0:
            try:
                self._loop.remove_reader(self._fd)
            except Exception:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1
