import json,subprocess


def test_runtime_prepares_local_outputs_only_when_enabled_and_due(tmp_path):
    from mailorganizer.runtime import init_config,run_once
    config=tmp_path/"runtime.json";init_config(config,tmp_path/"archive",tmp_path/"state")
    cfg=json.loads(config.read_text());cfg["auto_prepare"]=True;cfg["preparation_interval_seconds"]=900;config.write_text(json.dumps(cfg))
    calls=[]
    def runner(command,timeout):
        calls.append(command)
        if "prepare" in command:return subprocess.CompletedProcess(command,0,json.dumps({"status":"local_pipeline_prepared","messages":10,"documents":2,"calendar_modified":False}),"")
        return subprocess.CompletedProcess(command,0,json.dumps({"status":"gmail_sync_step_finished","state":"synced","completed":5,"pending":0,"failed":0,"inserted_this_step":1,"full_mailbox_verified":True}),"")
    cycle=run_once(config,runner=runner)
    assert cycle["preparation"]["state"] == "prepared"
    assert sum("prepare" in c for c in calls) == 1
    calls.clear();run_once(config,runner=runner)
    assert not any("prepare" in c for c in calls)
