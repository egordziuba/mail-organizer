from conftest import windows_only

import json
import subprocess
import pytest


def test_config_and_cycle_are_durable_and_account_errors_are_isolated(synthetic_base_interpreter, tmp_path):
    from mailorganizer.runtime import init_config, run_once, load_config
    config = tmp_path / "runtime.json"
    init_config(config, tmp_path / "archive", tmp_path / "state")
    assert load_config(config)["interval_seconds"] == 900
    with pytest.raises(ValueError):
        init_config(config, tmp_path / "other", tmp_path / "state")
    calls = []
    def synthetic_runner(command, timeout):
        calls.append(command)
        account = command[command.index("--account") + 1]
        if account == "gmail-1":
            return subprocess.CompletedProcess(command, 2, json.dumps({"status":"blocked","reason":"temporary_unavailable"}), "SYNTHETIC_PRIVATE_ERROR")
        return subprocess.CompletedProcess(command, 2, json.dumps({"status":"blocked","reason":"reauthorize_required"}), "")
    cycle = run_once(config, runner=synthetic_runner)
    assert cycle["accounts"]["gmail-1"]["state"] == "retry_later"
    assert cycle["accounts"]["gmail-2"]["state"] == "auth_required"
    assert cycle["accounts"]["outlook-1"]["state"] == "configuration_required"
    assert len(calls) == 2
    assert "SYNTHETIC_PRIVATE_ERROR" not in json.dumps(cycle)
    assert cycle["calendar_modified"] is False


def test_background_pause_skips_remaining_accounts(synthetic_base_interpreter, tmp_path):
    from mailorganizer import runtime
    config=tmp_path / "runtime.json"
    runtime.init_config(config,tmp_path / "archive",tmp_path / "state")
    calls=[]
    def runner(command,timeout):
        calls.append(command)
        state=tmp_path / "state";state.mkdir(exist_ok=True)
        (state / "paused.json").write_text("{}")
        return subprocess.CompletedProcess(command,0,json.dumps({"status":"bounded_readonly_scan_finished","downloaded":0,"inserted":0}),"")
    result=runtime.run_once(config,runner=runner,respect_pause=True)
    assert len(calls) == 1
    assert result["accounts"]["gmail-2"]["state"] == "paused"
    assert result["accounts"]["outlook-1"]["state"] == "paused"


@windows_only
def test_start_prefers_matching_windows_task_without_direct_spawn(tmp_path,monkeypatch):
    from mailorganizer import runtime
    config=tmp_path / "runtime.json"
    runtime.init_config(config,tmp_path / "archive",tmp_path / "state")
    ready=[False]
    def status(path):return {"running":ready[0],"heartbeat_fresh":ready[0],"pid":1234 if ready[0] else None,"paused":False}
    def schedule(path):ready[0]=True;return True
    monkeypatch.setattr(runtime,"worker_status",status)
    monkeypatch.setattr(runtime,"_start_scheduled_worker",schedule)
    monkeypatch.setattr(runtime.subprocess,"Popen",lambda *a,**k:pytest.fail("Matching task must own the worker"))
    assert runtime.start_worker(config)["launch_mode"] == "windows_task"



@windows_only
def test_crashed_hidden_process_releases_lock_and_can_be_raised(tmp_path):
    import time,sys,os
    from mailorganizer import runtime
    config=tmp_path / "runtime.json"
    runtime.init_config(config,tmp_path / "archive",tmp_path / "state")
    content=json.loads(config.read_text())
    for row in content["accounts"]:row["enabled"]=False
    config.write_text(json.dumps(content))
    from mailorganizer.processes import OwnedChild
    child=OwnedChild([runtime._python(False),"-E","-X","utf8","-m","mailorganizer.runtime","run","--config",str(config)])
    try:
        deadline=time.monotonic()+8
        while time.monotonic()<deadline and not runtime.worker_status(config)["running"]:
            time.sleep(0.1)
        worker_pid=runtime.worker_status(config)["pid"]
        assert child.has_pid(worker_pid)
        # Terminate only this Popen-owned synthetic worker, never a production PID.
        child.kill();child.wait(timeout=5)
        assert runtime.worker_status(config)["running"] is False
        raised=runtime.start_worker(config)
        assert raised["running"] is True
        assert raised["pid"] != worker_pid
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)
        runtime.stop_worker(config,wait_seconds=10)



@windows_only
def test_hidden_worker_start_duplicate_stop_and_resume(tmp_path):
    from mailorganizer.runtime import init_config, start_worker, stop_worker, worker_status
    config=tmp_path / "runtime.json"
    init_config(config,tmp_path / "archive",tmp_path / "state")
    isolated=json.loads(config.read_text())
    for row in isolated["accounts"]:row["enabled"]=False
    config.write_text(json.dumps(isolated))
    started=start_worker(config)
    try:
        assert started["running"] is True
        assert started["heartbeat_fresh"] is True
        duplicate=start_worker(config)
        assert duplicate["pid"] == started["pid"]
        stopped=stop_worker(config,wait_seconds=10)
        assert stopped["running"] is False
        assert stopped["paused"] is True
        # A scheduler's run invocation must honor the manual pause.
        import subprocess,sys
        result=subprocess.run([sys.executable,"-m","mailorganizer.runtime","run","--config",str(config)],capture_output=True,text=True,timeout=10)
        assert result.returncode == 0
        assert worker_status(config)["running"] is False
        restarted=start_worker(config)
        assert restarted["running"] is True
        assert restarted["pid"] != started["pid"]
    finally:
        stop_worker(config,wait_seconds=10)



@windows_only
def test_single_instance_lock_releases_without_stale_file(tmp_path):
    from mailorganizer.runtime import NamedMutex, WorkerBusy
    identity = tmp_path / "synthetic-runtime"
    with NamedMutex(identity):
        # Windows mutexes are recursive on the same thread: prove exclusion from another thread.
        import threading
        results=[]
        def attempt():
            try:
                with NamedMutex(identity): results.append("acquired")
            except WorkerBusy: results.append("busy")
        thread=threading.Thread(target=attempt)
        thread.start(); thread.join()
        assert results == ["busy"]
    with NamedMutex(identity):
        pass


def test_status_checks_process_identity_and_heartbeat(tmp_path,monkeypatch):
    from mailorganizer import runtime
    config=tmp_path / "runtime.json"
    runtime.init_config(config,tmp_path / "archive",tmp_path / "state")
    state=tmp_path / "state" / "worker.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"pid":1234,"birth":"wrong","generation":"synthetic","heartbeat":runtime._utc(),"state":"running"}))
    monkeypatch.setattr(runtime,"_birth",lambda pid:"current")
    assert runtime.worker_status(config)["running"] is False
    state.write_text(json.dumps({"pid":1234,"birth":"current","generation":"synthetic","heartbeat":"2000-01-01T00:00:00+00:00","state":"running"}))
    assert runtime.worker_status(config)["state"] == "stalled"

