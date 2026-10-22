from conftest import windows_only

import os,subprocess,struct
from pathlib import Path
import pytest


@windows_only
def test_hidden_command_uses_gui_base_interpreter_and_explicit_bootstrap():
    from mailorganizer.launching import project_command
    command=project_command("mailorganizer.runtime",["status","--config","runtime.json"],console=False)
    binary=Path(command[0]).read_bytes();pe=struct.unpack_from("<I",binary,0x3c)[0]
    assert struct.unpack_from("<H",binary,pe+24+68)[0] == 2
    assert "Scripts" not in Path(command[0]).parts
    assert any(x.endswith("worker_boot.pyw") for x in command)
    with pytest.raises(ValueError):project_command("os",[],console=False)


@windows_only
def test_bootstrap_imports_project_without_hermes_pythonpath():
    from mailorganizer.launching import project_command
    env=dict(os.environ);env.pop("PYTHONPATH",None);env.pop("PYTHONHOME",None)
    process=subprocess.run(project_command("mailorganizer.cli",["--help"]),env=env,capture_output=True,text=True,encoding="utf-8",timeout=15)
    assert process.returncode == 0
    assert "validate-results" in process.stdout
