"""Utilities common to Linux and Linux-like platforms (e.g. Cygwin)."""

from __future__ import division

import errno
import functools
import glob
import os
import re
from collections import namedtuple

from . import _common
from . import _psposix
from . import _psutil_posix as cext_posix
from ._common import bytes2str
from ._common import get_procfs_path
from ._common import memoize_when_activated
from ._common import open_binary
from ._common import open_text
from ._common import path_exists_strict
from ._compat import b
from ._compat import basestring


# =====================================================================
# --- globals
# =====================================================================

BOOT_TIME = None  # set later by boot_time()

# Number of clock ticks per second
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")

PAGESIZE = os.sysconf("SC_PAGE_SIZE")

# See:
# https://github.com/torvalds/linux/blame/master/fs/proc/array.c
# ...and (TASK_* constants):
# https://github.com/torvalds/linux/blob/master/include/linux/sched.h
PROC_STATUSES = {
    "R": _common.STATUS_RUNNING,
    "S": _common.STATUS_SLEEPING,
    "D": _common.STATUS_DISK_SLEEP,
    "T": _common.STATUS_STOPPED,
    "t": _common.STATUS_TRACING_STOP,
    "Z": _common.STATUS_ZOMBIE,
    "X": _common.STATUS_DEAD,
    "x": _common.STATUS_DEAD,
    "K": _common.STATUS_WAKE_KILL,
    "W": _common.STATUS_WAKING,
    "I": _common.STATUS_IDLE,
    "P": _common.STATUS_PARKED,
}


# =====================================================================
# --- named tuples
# =====================================================================


pmem = namedtuple('pmem', 'rss vms shared text lib data dirty')


# =====================================================================
# --- utils
# =====================================================================


def readlink(path):
    """Wrapper around os.readlink()."""
    assert isinstance(path, basestring), path
    path = os.readlink(path)
    # readlink() might return paths containing null bytes ('\x00')
    # resulting in "TypeError: must be encoded string without NULL
    # bytes, not str" errors when the string is passed to other
    # fs-related functions (os.*, open(), ...).
    # Apparently everything after '\x00' is garbage (we can have
    # ' (deleted)', 'new' and possibly others), see:
    # https://github.com/giampaolo/psutil/issues/717
    path = path.split('\x00')[0]
    # Certain paths have ' (deleted)' appended. Usually this is
    # bogus as the file actually exists. Even if it doesn't we
    # don't care.
    if path.endswith(' (deleted)') and not path_exists_strict(path):
        path = path[:-10]
    return path


# =====================================================================
# --- other system functions
# =====================================================================


def boot_time():
    """Return the system boot time expressed in seconds since the epoch."""
    global BOOT_TIME
    path = '%s/stat' % get_procfs_path()
    with open_binary(path) as f:
        for line in f:
            if line.startswith(b'btime'):
                ret = float(line.strip().split()[1])
                BOOT_TIME = ret
                return ret
        raise RuntimeError(
            "line 'btime' not found in %s" % path)


# =====================================================================
# --- CPU
# =====================================================================


def cpu_count_logical():
    """Return the number of logical CPUs in the system."""
    try:
        return os.sysconf("SC_NPROCESSORS_ONLN")
    except ValueError:
        # as a second fallback we try to parse /proc/cpuinfo
        num = 0
        with open_binary('%s/cpuinfo' % get_procfs_path()) as f:
            for line in f:
                if line.lower().startswith(b'processor'):
                    num += 1

        # unknown format (e.g. amrel/sparc architectures), see:
        # https://github.com/giampaolo/psutil/issues/200
        # try to parse /proc/stat as a last resort
        if num == 0:
            search = re.compile(r'cpu\d')
            with open_text('%s/stat' % get_procfs_path()) as f:
                for line in f:
                    line = line.split(' ')[0]
                    if search.match(line):
                        num += 1

        if num == 0:
            # mimic os.cpu_count()
            return None
        return num


def cpu_count_physical():
    """Return the number of physical cores in the system."""
    # Method #1
    core_ids = set()
    for path in glob.glob(
            "/sys/devices/system/cpu/cpu[0-9]*/topology/core_id"):
        with open_binary(path) as f:
            core_ids.add(int(f.read()))
    result = len(core_ids)
    if result != 0:
        return result

    # Method #2
    mapping = {}
    current_info = {}
    with open_binary('%s/cpuinfo' % get_procfs_path()) as f:
        for line in f:
            line = line.strip().lower()
            if not line:
                # new section
                if (b'physical id' in current_info and
                        b'cpu cores' in current_info):
                    mapping[current_info[b'physical id']] = \
                        current_info[b'cpu cores']
                current_info = {}
            else:
                # ongoing section
                if (line.startswith(b'physical id') or
                        line.startswith(b'cpu cores')):
                    key, value = line.split(b'\t:', 1)
                    current_info[key] = int(value)

    result = sum(mapping.values())
    return result or None  # mimic os.cpu_count()


def cpu_stats():
    """Return various CPU stats as a named tuple."""
    with open_binary('%s/stat' % get_procfs_path()) as f:
        ctx_switches = None
        interrupts = None
        soft_interrupts = None
        for line in f:
            if line.startswith(b'ctxt'):
                ctx_switches = int(line.split()[1])
            elif line.startswith(b'intr'):
                interrupts = int(line.split()[1])
            elif line.startswith(b'softirq'):
                soft_interrupts = int(line.split()[1])
            if ctx_switches is not None and soft_interrupts is not None \
                    and interrupts is not None:
                break
    syscalls = 0
    return _common.scpustats(
        ctx_switches, interrupts, soft_interrupts, syscalls)


# =====================================================================
# --- processes
# =====================================================================


def pids():
    """Returns a list of PIDs currently running on the system."""
    return [int(x) for x in os.listdir(b(get_procfs_path())) if x.isdigit()]


def pid_exists(pid):
    """Check for the existence of a unix PID. Linux TIDs are not
    supported (always return False).
    """
    if not _psposix.pid_exists(pid):
        return False
    else:
        # Linux's apparently does not distinguish between PIDs and TIDs
        # (thread IDs).
        # listdir("/proc") won't show any TID (only PIDs) but
        # os.stat("/proc/{tid}") will succeed if {tid} exists.
        # os.kill() can also be passed a TID. This is quite confusing.
        # In here we want to enforce this distinction and support PIDs
        # only, see:
        # https://github.com/giampaolo/psutil/issues/687
        try:
            # Note: already checked that this is faster than using a
            # regular expr. Also (a lot) faster than doing
            # 'return pid in pids()'
            path = "%s/%s/status" % (get_procfs_path(), pid)
            with open_binary(path) as f:
                for line in f:
                    if line.startswith(b"Tgid:"):
                        tgid = int(line.split()[1])
                        # If tgid and pid are the same then we're
                        # dealing with a process PID.
                        return tgid == pid
                raise ValueError("'Tgid' line not found in %s" % path)
        except (EnvironmentError, ValueError):
            return pid in pids()


def wrap_exceptions(fun):
    """Decorator which translates bare OSError and IOError exceptions
    into NoSuchProcess and AccessDenied.
    """
    @functools.wraps(fun)
    def wrapper(self, *args, **kwargs):
        try:
            return fun(self, *args, **kwargs)
        except EnvironmentError as err:
            if err.errno in (errno.EPERM, errno.EACCES):
                raise AccessDenied(self.pid, self._name)
            # ESRCH (no such process) can be raised on read() if
            # process is gone in the meantime.
            if err.errno == errno.ESRCH:
                raise NoSuchProcess(self.pid, self._name)
            # ENOENT (no such file or directory) can be raised on open().
            if err.errno == errno.ENOENT and not os.path.exists("%s/%s" % (
                    self._procfs_path, self.pid)):
                raise NoSuchProcess(self.pid, self._name)
            # Note: zombies will keep existing under /proc until they're
            # gone so there's no way to distinguish them in here.
            raise
    return wrapper


class Process(object):
    """Linux-like process implementation."""

    __slots__ = ["pid", "_name", "_ppid", "_procfs_path", "_cache"]

    def __init__(self, pid):
        self.pid = pid
        self._name = None
        self._ppid = None
        self._procfs_path = get_procfs_path()

    def _assert_alive(self):
        """Raise NSP if the process disappeared on us."""
        # For those C function who do not raise NSP, possibly returning
        # incorrect or incomplete result.
        os.stat('%s/%s' % (self._procfs_path, self.pid))

    @wrap_exceptions
    @memoize_when_activated
    def _parse_stat_file(self):
        """Parse /proc/{pid}/stat file and return a dict with various
        process info.
        Using "man proc" as a reference: where "man proc" refers to
        position N always substract 3 (e.g ppid position 4 in
        'man proc' == position 1 in here).
        The return value is cached in case oneshot() ctx manager is
        in use.
        """
        with open_binary("%s/%s/stat" % (self._procfs_path, self.pid)) as f:
            data = f.read()
        # Process name is between parentheses. It can contain spaces and
        # other parentheses. This is taken into account by looking for
        # the first occurrence of "(" and the last occurence of ")".
        rpar = data.rfind(b')')
        name = data[data.find(b'(') + 1:rpar]
        fields = data[rpar + 2:].split()

        ret = {}
        ret['name'] = name
        ret['status'] = fields[0]
        ret['ppid'] = fields[1]
        ret['ttynr'] = fields[4]
        ret['utime'] = fields[11]
        ret['stime'] = fields[12]
        ret['children_utime'] = fields[13]
        ret['children_stime'] = fields[14]
        ret['create_time'] = fields[19]
        ret['cpu_num'] = fields[36]

        return ret

    @wrap_exceptions
    @memoize_when_activated
    def _read_status_file(self):
        """Read /proc/{pid}/stat file and return its content.
        The return value is cached in case oneshot() ctx manager is
        in use.
        """
        with open_binary("%s/%s/status" % (self._procfs_path, self.pid)) as f:
            return f.read()

    def oneshot_enter(self):
        self._parse_stat_file.cache_activate(self)
        self._read_status_file.cache_activate(self)

    def oneshot_exit(self):
        self._parse_stat_file.cache_deactivate(self)
        self._read_status_file.cache_deactivate(self)

    @wrap_exceptions
    def name(self):
        return bytes2str(self._parse_stat_file()['name'])

    def exe(self):
        try:
            return readlink("%s/%s/exe" % (self._procfs_path, self.pid))
        except OSError as err:
            if err.errno in (errno.ENOENT, errno.ESRCH):
                # no such file error; might be raised also if the
                # path actually exists for system processes with
                # low pids (about 0-20)
                if os.path.lexists("%s/%s" % (self._procfs_path, self.pid)):
                    return ""
                else:
                    if not pid_exists(self.pid):
                        raise NoSuchProcess(self.pid, self._name)
                    else:
                        raise ZombieProcess(self.pid, self._name, self._ppid)
            if err.errno in (errno.EPERM, errno.EACCES):
                raise AccessDenied(self.pid, self._name)
            raise

    @wrap_exceptions
    def cmdline(self):
        with open_text("%s/%s/cmdline" % (self._procfs_path, self.pid)) as f:
            data = f.read()
        if not data:
            # may happen in case of zombie process
            return []
        # 'man proc' states that args are separated by null bytes '\0'
        # and last char is supposed to be a null byte. Nevertheless
        # some processes may change their cmdline after being started
        # (via setproctitle() or similar), they are usually not
        # compliant with this rule and use spaces instead. Google
        # Chrome process is an example. See:
        # https://github.com/giampaolo/psutil/issues/1179
        sep = '\x00' if data.endswith('\x00') else ' '
        if data.endswith(sep):
            data = data[:-1]
        return [x for x in data.split(sep)]

    @wrap_exceptions
    def cwd(self):
        try:
            return readlink("%s/%s/cwd" % (self._procfs_path, self.pid))
        except OSError as err:
            # https://github.com/giampaolo/psutil/issues/986
            if err.errno in (errno.ENOENT, errno.ESRCH):
                if not pid_exists(self.pid):
                    raise NoSuchProcess(self.pid, self._name)
                else:
                    raise ZombieProcess(self.pid, self._name, self._ppid)
            raise

    @wrap_exceptions
    def create_time(self):
        ctime = float(self._parse_stat_file()['create_time'])
        # According to documentation, starttime is in field 21 and the
        # unit is jiffies (clock ticks).
        # We first divide it for clock ticks and then add uptime returning
        # seconds since the epoch, in UTC.
        # Also use cached value if available.
        bt = BOOT_TIME or boot_time()
        return (ctime / CLOCK_TICKS) + bt

    @wrap_exceptions
    def cpu_times(self):
        values = self._parse_stat_file()
        utime = float(values['utime']) / CLOCK_TICKS
        stime = float(values['stime']) / CLOCK_TICKS
        children_utime = float(values['children_utime']) / CLOCK_TICKS
        children_stime = float(values['children_stime']) / CLOCK_TICKS
        return _common.pcputimes(utime, stime, children_utime, children_stime)

    @wrap_exceptions
    def nice_get(self):
        # with open_text('%s/%s/stat' % (self._procfs_path, self.pid)) as f:
        #   data = f.read()
        #   return int(data.split()[18])

        # Use C implementation
        return cext_posix.getpriority(self.pid)

    @wrap_exceptions
    def nice_set(self, value):
        return cext_posix.setpriority(self.pid, value)

    @wrap_exceptions
    def status(self):
        letter = bytes2str(self._parse_stat_file()['status'])
        # XXX is '?' legit? (we're not supposed to return it anyway)
        return PROC_STATUSES.get(letter, '?')

    @wrap_exceptions
    def memory_info(self):
        #  ============================================================
        # | FIELD  | DESCRIPTION                         | AKA  | TOP  |
        #  ============================================================
        # | rss    | resident set size                   |      | RES  |
        # | vms    | total program size                  | size | VIRT |
        # | shared | shared pages (from shared mappings) |      | SHR  |
        # | text   | text ('code')                       | trs  | CODE |
        # | lib    | library (unused in Linux 2.6)       | lrs  |      |
        # | data   | data + stack                        | drs  | DATA |
        # | dirty  | dirty pages (unused in Linux 2.6)   | dt   |      |
        #  ============================================================
        with open_binary("%s/%s/statm" % (self._procfs_path, self.pid)) as f:
            vms, rss, shared, text, lib, data, dirty = \
                [int(x) * PAGESIZE for x in f.readline().split()[:7]]
        return pmem(rss, vms, shared, text, lib, data, dirty)
