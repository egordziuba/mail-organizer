"""Generate a least-privilege Windows Task Scheduler definition; no registration on import."""
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET
import subprocess
from .launching import project_command

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
TASK_NAME = "MailOrganizer.HiddenWorker"


def build_task_xml(project, config, user_sid, start_boundary):
    project, config = Path(project).resolve(), Path(config).resolve()
    if '"' in str(config) or not isinstance(user_sid, str) or not user_sid.startswith("S-1-"):
        raise ValueError("Unsafe task identity/path")
    ET.register_namespace("", NS)
    def node(parent, name, text=None, **attributes):
        item=ET.SubElement(parent, "{"+NS+"}"+name, attributes)
        if text is not None:item.text=str(text)
        return item
    task=ET.Element("{"+NS+"}Task", {"version":"1.4"})
    registration=node(task,"RegistrationInfo")
    node(registration,"Author",user_sid)
    node(registration,"Description","Mail Organizer read-only hidden worker; owns no mail-send or calendar-write capability.")
    triggers=node(task,"Triggers")
    logon=node(triggers,"LogonTrigger")
    node(logon,"Enabled","true");node(logon,"UserId",user_sid);node(logon,"Delay","PT30S")
    time=node(triggers,"TimeTrigger")
    repeat=node(time,"Repetition")
    node(repeat,"Interval","PT5M");node(repeat,"StopAtDurationEnd","false")
    node(time,"StartBoundary",start_boundary);node(time,"Enabled","true")
    principals=node(task,"Principals")
    principal=node(principals,"Principal",id="Author")
    node(principal,"UserId",user_sid);node(principal,"LogonType","InteractiveToken");node(principal,"RunLevel","LeastPrivilege")
    settings=node(task,"Settings")
    for name,value in [("MultipleInstancesPolicy","IgnoreNew"),("DisallowStartIfOnBatteries","false"),("StopIfGoingOnBatteries","false"),("AllowHardTerminate","true"),("StartWhenAvailable","true"),("RunOnlyIfNetworkAvailable","false")]:node(settings,name,value)
    idle=node(settings,"IdleSettings");node(idle,"StopOnIdleEnd","false");node(idle,"RestartOnIdle","false")
    for name,value in [("AllowStartOnDemand","true"),("Enabled","true"),("Hidden","true"),("RunOnlyIfIdle","false"),("WakeToRun","false"),("ExecutionTimeLimit","PT0S"),("Priority","7")]:node(settings,name,value)
    restart=node(settings,"RestartOnFailure");node(restart,"Interval","PT1M");node(restart,"Count","3")
    actions=node(task,"Actions",Context="Author")
    action=node(actions,"Exec")
    command=project_command("mailorganizer.runtime",["run","--config",str(config)],console=False,project=project)
    node(action,"Command",command[0])
    node(action,"Arguments",subprocess.list2cmdline(command[1:]))
    node(action,"WorkingDirectory",str(project))
    return ET.tostring(task,encoding="unicode",xml_declaration=False)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--project",type=Path,required=True)
    parser.add_argument("--config",type=Path,required=True)
    parser.add_argument("--user-sid",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    from .runtime import load_config
    load_config(args.config)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    boundary=(datetime.now().astimezone()+timedelta(minutes=1)).isoformat(timespec="seconds")
    args.output.write_text(build_task_xml(args.project,args.config,args.user_sid,boundary),encoding="utf-8")


if __name__ == "__main__":
    main()
