#define WIN32_LEAN_AND_MEAN

#include <Python.h>
#include <windows.h>
#include <winsock2.h>
#include <iprtrmib.h>

#include <sys/cygwin.h>

#include "_psutil_common.h"
#include "arch/windows/disk.h"
#include "arch/windows/net.h"
#include "arch/windows/socks.h"


/*
 * stubs for functions used at the module-level by _pswindows but not needed
 * for the _pscygwin module
 */
#define NOT_IMPLEMENTED_STUB(NAME) \
    static PyObject * \
    psutil_##NAME(PyObject *self, PyObject *args) { \
        PyErr_SetString(PyExc_NotImplementedError, \
                        #NAME " not implemented by this module"); \
        return NULL; \
    }

NOT_IMPLEMENTED_STUB(pids)
NOT_IMPLEMENTED_STUB(pid_exists)
NOT_IMPLEMENTED_STUB(ppid_map)


/*
 * Convert the Cygwin PID of a process to/from its corresponding Windows PID
 */
static PyObject*
psutil_cygpid_to_winpid(PyObject *self, PyObject *args) {
    pid_t pid;
    DWORD winpid;

    if (! PyArg_ParseTuple(args, _Py_PARSE_PID, &pid))
        return NULL;

    if (!(winpid = (DWORD)cygwin_internal(CW_CYGWIN_PID_TO_WINPID, pid)))
        return NoSuchProcess("cygwin_internal");

#if PY_MAJOR_VERSION >= 3
    return PyLong_FromLong((long) winpid);
#else
    return PyInt_FromLong((long) winpid);
#endif
}


static PyObject*
psutil_winpid_to_cygpid(PyObject *self, PyObject *args) {
    pid_t pid;
    DWORD winpid;

    if (! PyArg_ParseTuple(args, _Py_PARSE_PID, &winpid))
        return NULL;

    /* For some reason (perhaps historical) Cygwin provides a function
     * specifically for this purpose, rather than using cygwin_internal
     * as in the opposite case. */
    if ((pid = cygwin_winpid_to_pid(winpid)) < 0)
        return NoSuchProcess("cygwin_winpid_to_pid");

#if PY_MAJOR_VERSION >= 3
    return PyLong_FromLong((long) pid);
#else
    return PyInt_FromLong((long) pid);
#endif
}


// Some defines for Bytes functions on Python 2 to keep the implementation
// of psutil_conv_path cleaner
#if PY_MAJOR_VERSION >= 3
#define _PyUnicode_AsWideChar PyUnicode_AsWideChar
#else
#define _PyUnicode_AsWideChar(obj, w, size) \
    PyUnicode_AsWideChar((PyUnicodeObject*)obj, w, size)
#define PyBytes_Check PyString_Check
#define PyBytes_FromStringAndSize PyString_FromStringAndSize
#define PyBytes_AS_STRING PyString_AS_STRING
#define _PyBytes_Resize _PyString_Resize
#define Py_FileSystemDefaultEncodeErrors "strict"
#endif


/*
 * Wrapper for cygwin_conv_path to convert between Cygwin/Windows paths.
 * The "what" argument is the same as the one taken by cygwin_conv_path,
 * and can be one of:
 *
 *  * CCP_WIN_A_TO_POSIX,
 *  * CCP_WIN_W_TO_POSIX,
 *  * CCP_POSIX_TO_WIN_W,
 *  * CCP_POSIX_TO_WIN_A
 *
 * Specifying what type of path the "path" argument is expected to be, and
 * what to convert it to.
 */
static PyObject*
psutil_conv_path(PyObject *self, PyObject *args) {
    int what, mode;
    PyObject *pathobj, *pathbytes = NULL, *tobytes = NULL, *res = NULL;
    void *path, *to;
    ssize_t size;
    int decode = 0;

    if (!PyArg_ParseTuple(args, "iO", &what, &pathobj))
        return NULL;

    // & out other modifiers to the "what" argument to get the basic
    // conversion mode
    mode = what & 0xff;

    // If the path object was unicode always return a unicode str
    if (PyUnicode_Check(pathobj)) {
        if (mode == CCP_WIN_W_TO_POSIX) {
            size = PyUnicode_GET_SIZE(pathobj) + 1;

            // Size in bytes to create a bytes object from
            pathbytes = PyBytes_FromStringAndSize(NULL,
                                                  size * sizeof(wchar_t));
            if (pathbytes == NULL)
                goto error;

            if (_PyUnicode_AsWideChar(pathobj,
                                      (wchar_t*) PyBytes_AS_STRING(pathbytes),
                                      size) < 0)
                goto error;
        } else {
            pathbytes = PyUnicode_AsEncodedString(pathobj,
                Py_FileSystemDefaultEncoding,
                Py_FileSystemDefaultEncodeErrors);
        }
        path = (void *)PyBytes_AS_STRING(pathbytes);
        decode = 1;
    } else if (PyBytes_Check(pathobj)) {
        path = PyBytes_AS_STRING(pathobj);
    } else {
#if PY_MAJOR_VERSION >= 3
        PyErr_SetString(PyExc_ValueError, "str or bytes expected");
#else
        PyErr_SetString(PyExc_ValueError, "unicode or str expected");
#endif
        goto error;
    }

    // First call with NULL for the output string to determine the required
    // output size
    size = cygwin_conv_path(what, path, NULL, 0);
    if (size < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        goto error;
    }

    tobytes = PyBytes_FromStringAndSize(NULL, size);
    to = PyBytes_AS_STRING(tobytes);

    if (cygwin_conv_path(what, path, to, size) < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        goto error;
    }

    if (decode) {
        if (mode == CCP_POSIX_TO_WIN_W) {
            // It's not totally clear from the documentation, but
            // PyUnicode_FromWideChar wants the size in characters excluding
            // the trailing null.
            res = PyUnicode_FromWideChar((wchar_t*) to,
                                         size / sizeof(wchar_t) - 1);
        } else {
            res = PyUnicode_Decode(to, size - 1,
                Py_FileSystemDefaultEncoding,
                Py_FileSystemDefaultEncodeErrors);
        }
    } else {
        // Just directly return the bytes value minus the terminal null(s)
        if (mode == CCP_POSIX_TO_WIN_W) {
            size -= sizeof(wchar_t);
        } else {
            size -= 1;
        }
        if (_PyBytes_Resize(&tobytes, size) == 0) {
            res = tobytes;
            Py_INCREF(res);
        }
    }

error:
    Py_XDECREF(pathbytes);
    Py_XDECREF(tobytes);
    return res;
}


/*
 * define the psutil C module methods and initialize the module.
 */
static PyMethodDef
PsutilMethods[] = {
    // --- system-related functions
    {"disk_io_counters", psutil_disk_io_counters, METH_VARARGS,
     "Return dict of tuples of disks I/O information."},
    {"net_connections", psutil_net_connections, METH_VARARGS,
     "Return system-wide connections"},
    {"net_if_addrs", psutil_net_if_addrs, METH_VARARGS,
     "Return NICs addresses."},
    {"net_if_stats", psutil_net_if_stats, METH_VARARGS,
     "Return NICs stats."},
    {"net_io_counters", psutil_net_io_counters, METH_VARARGS,
     "Return dict of tuples of networks I/O information."},

    // --- cygwin-specific functions
    {"cygpid_to_winpid", psutil_cygpid_to_winpid, METH_VARARGS,
     "Convert the Cygwin PID of a process to its corresponding Windows PID."},
    {"winpid_to_cygpid", psutil_winpid_to_cygpid, METH_VARARGS,
     "Convert the Windows PID of a process to its corresponding Cygwin PID."},
    {"conv_path", psutil_conv_path, METH_VARARGS,
     "Convert between Windows paths and Cygwin paths."},

    // --- others
    {"set_testing", psutil_set_testing, METH_NOARGS,
     "Set psutil in testing mode"},

    // --- not implemented stubs
    {"pids", psutil_pids, METH_VARARGS,
     "Returns a list of PIDs currently running on the system"},
    {"pid_exists", psutil_pid_exists, METH_VARARGS,
     "Determine if the process exists in the current process list."},
    {"ppid_map", psutil_ppid_map, METH_VARARGS,
     "Return a {pid:ppid, ...} dict for all running processes"},

    {NULL, NULL, 0, NULL}
};

struct module_state {
    PyObject *error;
};

#if PY_MAJOR_VERSION >= 3
#define GETSTATE(m) ((struct module_state*)PyModule_GetState(m))
#else
#define GETSTATE(m) (&_state)
#endif

#if PY_MAJOR_VERSION >= 3

static int
psutil_cygwin_traverse(PyObject *m, visitproc visit, void *arg) {
    Py_VISIT(GETSTATE(m)->error);
    return 0;
}

static int
psutil_cygwin_clear(PyObject *m) {
    Py_CLEAR(GETSTATE(m)->error);
    return 0;
}

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT,
    "psutil_cygwin",
    NULL,
    sizeof(struct module_state),
    PsutilMethods,
    NULL,
    psutil_cygwin_traverse,
    psutil_cygwin_clear,
    NULL
};

#define INITERROR return NULL

PyMODINIT_FUNC PyInit__psutil_cygwin(void)

#else
#define INITERROR return

void init_psutil_cygwin(void)
#endif
{
#if PY_MAJOR_VERSION >= 3
    PyObject *module = PyModule_Create(&moduledef);
#else
    PyObject *module = Py_InitModule("_psutil_cygwin", PsutilMethods);
#endif

    // version constant
    PyModule_AddIntConstant(module, "version", PSUTIL_VERSION);

    // Cygwin constants
    PyModule_AddIntConstant(
        module, "CCP_WIN_W_TO_POSIX", CCP_WIN_W_TO_POSIX);
    PyModule_AddIntConstant(
        module, "CCP_POSIX_TO_WIN_W", CCP_POSIX_TO_WIN_W);
    PyModule_AddIntConstant(
        module, "CCP_WIN_A_TO_POSIX", CCP_WIN_A_TO_POSIX);
    PyModule_AddIntConstant(
        module, "CCP_POSIX_TO_WIN_A", CCP_POSIX_TO_WIN_A);

    // process status constants
    // http://msdn.microsoft.com/en-us/library/ms683211(v=vs.85).aspx
    PyModule_AddIntConstant(
        module, "ABOVE_NORMAL_PRIORITY_CLASS", ABOVE_NORMAL_PRIORITY_CLASS);
    PyModule_AddIntConstant(
        module, "BELOW_NORMAL_PRIORITY_CLASS", BELOW_NORMAL_PRIORITY_CLASS);
    PyModule_AddIntConstant(
        module, "HIGH_PRIORITY_CLASS", HIGH_PRIORITY_CLASS);
    PyModule_AddIntConstant(
        module, "IDLE_PRIORITY_CLASS", IDLE_PRIORITY_CLASS);
    PyModule_AddIntConstant(
        module, "NORMAL_PRIORITY_CLASS", NORMAL_PRIORITY_CLASS);
    PyModule_AddIntConstant(
        module, "REALTIME_PRIORITY_CLASS", REALTIME_PRIORITY_CLASS);

    // connection status constants
    // http://msdn.microsoft.com/en-us/library/cc669305.aspx
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_CLOSED", MIB_TCP_STATE_CLOSED);
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_CLOSING", MIB_TCP_STATE_CLOSING);
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_CLOSE_WAIT", MIB_TCP_STATE_CLOSE_WAIT);
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_LISTEN", MIB_TCP_STATE_LISTEN);
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_ESTAB", MIB_TCP_STATE_ESTAB);
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_SYN_SENT", MIB_TCP_STATE_SYN_SENT);
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_SYN_RCVD", MIB_TCP_STATE_SYN_RCVD);
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_FIN_WAIT1", MIB_TCP_STATE_FIN_WAIT1);
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_FIN_WAIT2", MIB_TCP_STATE_FIN_WAIT2);
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_LAST_ACK", MIB_TCP_STATE_LAST_ACK);
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_TIME_WAIT", MIB_TCP_STATE_TIME_WAIT);
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_TIME_WAIT", MIB_TCP_STATE_TIME_WAIT);
    PyModule_AddIntConstant(
        module, "MIB_TCP_STATE_DELETE_TCB", MIB_TCP_STATE_DELETE_TCB);
    PyModule_AddIntConstant(
        module, "PSUTIL_CONN_NONE", PSUTIL_CONN_NONE);

    // ...for internal use in _psutil_windows.py
    PyModule_AddIntConstant(
        module, "ERROR_ACCESS_DENIED", ERROR_ACCESS_DENIED);
    PyModule_AddIntConstant(
        module, "ERROR_PRIVILEGE_NOT_HELD", ERROR_PRIVILEGE_NOT_HELD);

    if (module == NULL)
        INITERROR;
#if PY_MAJOR_VERSION >= 3
    return module;
#endif
}
