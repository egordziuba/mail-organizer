from conftest import windows_only

import ctypes,json,threading,time
from ctypes import wintypes


@windows_only
def test_status_replace_tolerates_a_brief_windows_reader_lock(tmp_path):
    from mailorganizer.cli import _write_json
    file=tmp_path / "worker.json"
    file.write_text('{"old":true}')
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW(str(file),0x80000000,3,None,3,0x80,None)
    assert handle != wintypes.HANDLE(-1).value
    released=threading.Event()
    def unlock():
        time.sleep(.08)
        kernel.CloseHandle(handle)
        released.set()
    thread=threading.Thread(target=unlock);thread.start()
    try:
        _write_json(file,{"new":True})
        assert json.loads(file.read_text()) == {"new":True}
    finally:
        thread.join()
