"""Own a Windows process tree, including venv launchers; kill no unrelated PID."""
import ctypes
from ctypes import wintypes
import os
import subprocess


class _BasicLimits(ctypes.Structure):
    _fields_=[("PerProcessUserTimeLimit",ctypes.c_longlong),("PerJobUserTimeLimit",ctypes.c_longlong),("LimitFlags",wintypes.DWORD),("MinimumWorkingSetSize",ctypes.c_size_t),("MaximumWorkingSetSize",ctypes.c_size_t),("ActiveProcessLimit",wintypes.DWORD),("Affinity",ctypes.c_size_t),("PriorityClass",wintypes.DWORD),("SchedulingClass",wintypes.DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_=[(name,ctypes.c_ulonglong) for name in ("ReadOperationCount","WriteOperationCount","OtherOperationCount","ReadTransferCount","WriteTransferCount","OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_=[("BasicLimitInformation",_BasicLimits),("IoInfo",_IOCounters),("ProcessMemoryLimit",ctypes.c_size_t),("JobMemoryLimit",ctypes.c_size_t),("PeakProcessMemoryUsed",ctypes.c_size_t),("PeakJobMemoryUsed",ctypes.c_size_t)]


class OwnedChild:
    """Assign a suspended child before resume, so descendants cannot escape the job."""
    def __init__(self, command, *, capture_output=False, env=None):
        if os.name != "nt":raise RuntimeError("Windows process supervision required")
        self.job=None; self.process=None
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR];kernel.CreateJobObjectW.restype=wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
        self.kernel=kernel
        job=kernel.CreateJobObjectW(None,None)
        if not job:raise OSError("Owned process job could not be created")
        self.job=job
        try:
            limits=_ExtendedLimits();limits.BasicLimitInformation.LimitFlags=0x2000
            if not kernel.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits)):
                raise OSError("Owned process limits could not be set")
            stream=subprocess.PIPE if capture_output else subprocess.DEVNULL
            self.process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=stream,stderr=stream,
                text=True,encoding="utf-8",errors="replace",env=env,
                creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
            if not kernel.AssignProcessToJobObject(job,int(self.process._handle)):
                raise OSError("Owned process could not be assigned")
            native=ctypes.WinDLL("ntdll")
            native.NtResumeProcess.argtypes=[wintypes.HANDLE];native.NtResumeProcess.restype=ctypes.c_long
            if native.NtResumeProcess(int(self.process._handle)) != 0:
                raise OSError("Owned process could not be resumed")
        except Exception:
            self.kill()
            if self.process is not None:
                if self.process.poll() is None:self.process.kill()
                self.process.wait(timeout=5)
            raise

    @property
    def pid(self):return self.process.pid

    def has_pid(self,pid):
        if self.job is None:return False
        handle=self.kernel.OpenProcess(0x1000,False,pid)
        if not handle:return False
        try:
            answer=wintypes.BOOL()
            return bool(self.kernel.IsProcessInJob(handle,self.job,ctypes.byref(answer)) and answer.value)
        finally:self.kernel.CloseHandle(handle)

    def poll(self):return self.process.poll()
    def wait(self,timeout=None):return self.process.wait(timeout=timeout)

    def kill(self):
        if self.job is not None:
            self.kernel.CloseHandle(self.job)
            self.job=None

    def __enter__(self):return self
    def __exit__(self,*args):
        self.kill()
        if self.process is not None:
            self.process.wait(timeout=5)
