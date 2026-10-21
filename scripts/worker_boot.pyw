"""Explicit project/venv bootstrap for a genuine base GUI interpreter."""
import os,site,sys,runpy,argparse
from pathlib import Path
root=Path(__file__).resolve().parents[1]
os.environ.pop("PYTHONHOME",None)
os.environ.pop("PYTHONPATH",None)
os.environ["VIRTUAL_ENV"]=str(root/".venv")
site.addsitedir(str(root/".venv"/"Lib"/"site-packages"))
sys.path.insert(0,str(root/"src"))
parser=argparse.ArgumentParser()
parser.add_argument("--module",choices=["mailorganizer.runtime","mailorganizer.cli"],default="mailorganizer.runtime")
args,remainder=parser.parse_known_args()
if remainder and remainder[0]=="--":remainder=remainder[1:]
sys.argv=[args.module,*remainder]
runpy.run_module(args.module,run_name="__main__")
