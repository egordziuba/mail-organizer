from conftest import windows_only

import xml.etree.ElementTree as ET


@windows_only
def test_windows_task_is_hidden_least_privileged_and_self_restarting(tmp_path):
    from mailorganizer.windows_task import build_task_xml
    root=tmp_path / "project"
    xml=build_task_xml(root,root / "runtime.json","S-1-5-21-SYNTHETIC","2026-10-10T00:00:00")
    tree=ET.fromstring(xml);ns={"t":"http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert tree.find(".//t:Command",ns).text.endswith("pythonw.exe")
    arguments=tree.find(".//t:Arguments",ns).text
    assert "worker_boot.pyw" in arguments
    assert "--module mailorganizer.runtime -- run" in arguments
    assert "Scripts" not in __import__("pathlib").Path(tree.find(".//t:Command",ns).text).parts
    assert tree.find(".//t:LogonType",ns).text == "InteractiveToken"
    assert tree.find(".//t:RunLevel",ns).text == "LeastPrivilege"
    assert tree.find(".//t:Hidden",ns).text == "true"
    assert tree.find(".//t:MultipleInstancesPolicy",ns).text == "IgnoreNew"
    assert tree.find(".//t:RestartOnFailure/t:Interval",ns).text == "PT1M"
    assert tree.find(".//t:TimeTrigger/t:Repetition/t:Interval",ns).text == "PT5M"
    assert tree.find(".//t:LogonTrigger",ns) is not None
    assert "password" not in xml.lower()
    assert not xml.startswith("<?xml"), "Task XML is passed as a Unicode string; no contradictory encoding declaration"
