"""Use real base interpreters, not uv Windows console launchers."""
from pathlib import Path
import sys

MODULES={"mailorganizer.cli","mailorganizer.runtime"}


def base_interpreter(project:Path,*,console=True):
    config=Path(project)/".venv"/"pyvenv.cfg"
    values={}
    if config.exists():
        for line in config.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key,value=line.split("=",1);values[key.strip()]=value.strip()
    home=Path(values.get("home",Path(sys._base_executable).parent))
    executable=home/("python.exe" if console else "pythonw.exe")
    if not executable.is_file():raise ValueError("Verified base Python unavailable")
    return executable


def project_command(module,args,*,console=True,project=None):
    if module not in MODULES:raise ValueError("Only fixed mail-organizer modules allowed")
    project=Path(project or Path(__file__).resolve().parents[2]).resolve()
    return [str(base_interpreter(project,console=console)),"-E","-X","utf8",str(project/"scripts"/"worker_boot.pyw"),"--module",module,"--",*map(str,args)]
