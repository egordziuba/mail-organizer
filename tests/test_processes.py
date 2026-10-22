from conftest import windows_only

import subprocess,sys,time
from pathlib import Path
import pytest


@windows_only
def test_timeout_terminates_owned_descendants_not_only_launcher(tmp_path):
    from mailorganizer.runtime import _runner,_birth
    marker,stop=tmp_path / "child.pid",tmp_path / "stop"
    child_code="import os,time;from pathlib import Path;Path("+repr(str(marker))+").write_text(str(os.getpid()));end=time.monotonic()+10\nwhile time.monotonic()<end and not Path("+repr(str(stop))+").exists():time.sleep(.05)"
    root_code="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',"+repr(child_code)+"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW);time.sleep(10)"
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            _runner([sys._base_executable,"-c",root_code],2)
        assert marker.exists()
        assert _birth(int(marker.read_text())) is None
    finally:
        stop.write_text("stop synthetic child")
