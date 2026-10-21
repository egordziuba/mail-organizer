import json,sys,types,subprocess


def test_cli_uses_resumable_gmail_engine_not_pilot(monkeypatch,tmp_path,capsys):
    from mailorganizer.cli import main
    from mailorganizer import auth,connectors
    module=types.ModuleType("mailorganizer.gmail_sync")
    calls=[]
    class Engine:
        def __init__(self,root,account_id,reader,**kwargs):calls.append((root,account_id,kwargs))
        def step(self,**kwargs):
            assert kwargs["max_messages"] == 123
            return {"status":"gmail_sync_step_finished","state":"backfill_pending","catalog_complete":True,"pending":500,"completed":123,"deleted":0,"failed":0,"fetched_this_step":123,"inserted_this_step":123,"full_mailbox_verified":False}
    module.GmailSync=Engine;monkeypatch.setitem(sys.modules,"mailorganizer.gmail_sync",module)
    monkeypatch.setattr(auth,"google_access_token",lambda account:"SYNTHETIC")
    monkeypatch.setattr(connectors,"GmailReader",lambda token:object())
    assert main(["sync","--root",str(tmp_path),"--account","gmail-1","--gmail-mode","incremental","--include-spam-trash","--max-messages","123"]) == 0
    assert len(calls) == 1
    result=json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["pending"] == 500
    assert result["full_mailbox_verified"] is False


def test_runtime_keeps_backfill_progress_and_uses_incremental_command(tmp_path):
    from mailorganizer.runtime import init_config,run_once
    config=tmp_path/"runtime.json";init_config(config,tmp_path/"archive",tmp_path/"state")
    def runner(command,timeout):
        assert "--gmail-mode" in command
        assert "incremental" in command
        assert "--include-spam-trash" in command
        return subprocess.CompletedProcess(command,0,json.dumps({"status":"gmail_sync_step_finished","state":"backfill_pending","pending":333,"completed":20,"failed":0,"catalog_complete":True,"full_mailbox_verified":False}),"")
    cycle=run_once(config,runner=runner)
    assert cycle["accounts"]["gmail-1"]["state"] == "backfill_pending"
    assert cycle["accounts"]["gmail-1"]["pending"] == 333
