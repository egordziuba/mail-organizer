"""Durable command-driven worker. No interactive auth, model calls or calendar writes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from .cli import _write_json

ACCOUNTS = ("gmail-1", "gmail-2", "outlook-1")


def _utc():
    return datetime.now(timezone.utc).isoformat()


def init_config(path, archive_root, state_dir):
    path = Path(path).resolve()
    if path.exists():
        raise ValueError("Existing runtime configuration is not overwritten")
    config = {"schema_version":1, "archive_root":str(Path(archive_root).resolve()),
              "state_dir":str(Path(state_dir).resolve()), "interval_seconds":900,
              "subprocess_timeout_seconds":180,
              "accounts":[{"id":a, "enabled":True, "max_messages":100, "query":"", "folder":"inbox", "client_id":None} for a in ACCOUNTS]}
    _validate(config)
    _write_json(path, config)
    return {"status":"configuration_created", "config":str(path), "contains_tokens":False}


def _validate(config):
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("Unsupported runtime schema")
    for field in ("interval_seconds", "subprocess_timeout_seconds"):
        if type(config.get(field)) is not int or not 1 <= config[field] <= 86400:
            raise ValueError("Invalid runtime timing")
    archive, state = Path(config["archive_root"]), Path(config["state_dir"])
    if not archive.is_absolute() or not state.is_absolute() or archive.resolve() == state.resolve() or state.resolve().is_relative_to(archive.resolve() / "messages"):
        raise ValueError("Separate absolute archive and state paths required")
    rows = config.get("accounts")
    if not isinstance(rows, list) or len(rows) != 3 or {r.get("id") for r in rows} != set(ACCOUNTS):
        raise ValueError("Exactly three distinct account slots required")
    for row in rows:
        if type(row.get("enabled")) is not bool or type(row.get("max_messages")) is not int or not 1 <= row["max_messages"] <= 10000:
            raise ValueError("Invalid account policy")
        if not isinstance(row.get("query"), str) or not isinstance(row.get("folder"), str):
            raise ValueError("Invalid mailbox scope")
        if row.get("client_id") is not None and not isinstance(row["client_id"], str):
            raise ValueError("Public client ID must be a string")
    dataset = archive / "dataset.json"
    if dataset.exists() and json.loads(dataset.read_text(encoding="utf-8")).get("kind") == "synthetic":
        raise ValueError("Real worker refuses a synthetic archive")


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate(config)
    return config


def _environment():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _python(console=True):
    executable = Path(sys.executable)
    if os.name == "nt":
        executable = executable.with_name("python.exe" if console else "pythonw.exe")
    if not executable.is_file():
        raise ValueError("Project Python executable is unavailable")
    return str(executable)


def _runner(command, timeout):
    from .processes import OwnedChild
    with OwnedChild(command, capture_output=True, env=_environment()) as child:
        try:
            stdout, stderr = child.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.process.communicate(timeout=5)
            raise
        return subprocess.CompletedProcess(command, child.process.returncode, stdout, stderr)


def run_once(path, runner=None, *, respect_pause=False):
    config = load_config(path)
    run = runner or _runner
    cycle = {"started_at":_utc(), "accounts":{}, "calendar_modified":False, "mailbox_modified":False}
    for row in config["accounts"]:
        account = row["id"]
        if respect_pause and (Path(config["state_dir"]) / "paused.json").exists():
            result = {"state":"paused"}
        elif not row["enabled"]:
            result = {"state":"disabled"}
        elif account == "outlook-1" and not row["client_id"]:
            result = {"state":"configuration_required", "reason":"public_client_id_missing"}
        else:
            from .launching import project_command
            arguments = ["sync", "--root", config["archive_root"], "--account", account, "--max-messages", str(row["max_messages"])]
            if account == "outlook-1":
                arguments += ["--client-id", row["client_id"], "--folder", row["folder"]]
            else:
                arguments += ["--query", row["query"], "--gmail-mode", row.get("gmail_mode","incremental"),
                              "--time-budget-seconds", str(row.get("time_budget_seconds",120)), "--workers", str(row.get("workers",4))]
                if row.get("include_spam_trash",True):arguments.append("--include-spam-trash")
            command = project_command("mailorganizer.cli", arguments)
            try:
                process = run(command, config["subprocess_timeout_seconds"])
                if not process.stdout or len(process.stdout) > 200000:
                    raise ValueError("Invalid worker child response")
                response = json.loads(process.stdout.strip().splitlines()[-1])
                if not isinstance(response, dict):
                    raise ValueError("Invalid worker child response")
                if process.returncode == 0 and response.get("status") == "gmail_sync_step_finished":
                    state = response.get("state")
                    result = {"state":state if state in {"backfill_pending","synced","needs_attention","disk_space_blocked"} else "needs_attention",
                              "full_mailbox_verified":response.get("full_mailbox_verified") is True,
                              "catalog_complete":response.get("catalog_complete") is True}
                    for field in ("pending","completed","deleted","failed","fetched_this_step","inserted_this_step"):
                        value=response.get(field,0)
                        result[field]=value if type(value) is int and value>=0 else 0
                elif process.returncode == 0 and response.get("status") == "bounded_readonly_scan_finished":
                    result = {"state":"bounded_scan_finished", "full_mailbox_verified":False}
                    for field in ("downloaded", "inserted"):
                        value = response.get(field, 0)
                        result[field] = value if type(value) is int and value >= 0 else 0
                else:
                    reason = response.get("reason")
                    state = {"reauthorize_required":"auth_required", "temporary_unavailable":"retry_later", "configuration_error":"configuration_required"}.get(reason, "needs_attention")
                    result = {"state":state, "reason":reason if reason in {"reauthorize_required", "temporary_unavailable", "configuration_error"} else "child_failed"}
            except subprocess.TimeoutExpired:
                result = {"state":"retry_later", "reason":"child_timeout"}
            except Exception:
                result = {"state":"needs_attention", "reason":"child_response_or_launch_failed"}
        cycle["accounts"][account] = result
    if config.get("auto_prepare",False) and any(v.get("completed",0)>0 for v in cycle["accounts"].values()):
        import time
        marker=Path(config["state_dir"])/"preparation_marker.json"
        previous=json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
        if time.time()-previous.get("prepared_at",0)>=config.get("preparation_interval_seconds",900):
            from .launching import project_command
            try:
                response=run(project_command("mailorganizer.cli",["prepare","--root",config["archive_root"]]),config.get("preparation_timeout_seconds",300))
                body=json.loads(response.stdout.strip().splitlines()[-1])
                if response.returncode==0 and body.get("status")=="local_pipeline_prepared":
                    cycle["preparation"]={"state":"prepared","messages":body.get("messages",0),"documents":body.get("documents",0)}
                    _write_json(marker,{"prepared_at":time.time()})
                else:cycle["preparation"]={"state":"needs_attention"}
            except Exception:cycle["preparation"]={"state":"needs_attention"}
    cycle["finished_at"] = _utc()
    _write_json(Path(config["state_dir"]) / "last_cycle.json", cycle)
    return cycle


class WorkerBusy(RuntimeError):
    pass


class NamedMutex:
    """Kernel-owned lock: process exit/crash releases it; no stale PID-file lock."""
    def __init__(self, identity):
        import hashlib
        self.name = "Local\\MailOrganizer." + hashlib.sha256(str(Path(identity).resolve()).casefold().encode()).hexdigest()
        self.handle = None

    def __enter__(self):
        import ctypes
        from ctypes import wintypes
        if os.name != "nt":
            raise RuntimeError("Windows runtime required")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError("Could not create worker mutex")
        result = kernel.WaitForSingleObject(handle, 0)
        if result not in (0, 128):
            kernel.CloseHandle(handle)
            raise WorkerBusy("Another worker owns this configuration")
        self.handle, self.kernel = handle, kernel
        return self

    def __exit__(self, *args):
        from ctypes import wintypes
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.ReleaseMutex(self.handle)
        self.kernel.CloseHandle(self.handle)
        self.handle = None


def _birth(pid):
    """Live process creation time prevents PID reuse being mistaken for ownership."""
    import ctypes
    from ctypes import wintypes
    if type(pid) is not int or pid <= 0 or os.name != "nt":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def worker_status(path):
    config = load_config(path)
    directory = Path(config["state_dir"])
    paused = (directory / "paused.json").exists()
    file = directory / "worker.json"
    if not file.exists():
        return {"state":"paused" if paused else "not_started", "running":False, "paused":paused}
    try:
        state = json.loads(file.read_text(encoding="utf-8"))
        live_birth = _birth(state.get("pid"))
        alive = bool(live_birth and live_birth == state.get("birth"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["heartbeat"])).total_seconds()
        healthy = alive and 0 <= age <= 30
    except (ValueError, TypeError, KeyError, OSError):
        return {"state":"invalid_status", "running":False, "paused":paused}
    return {"state":("running" if healthy else "stalled" if alive else "paused" if paused else "stopped"),
            "running":alive, "heartbeat_fresh":healthy, "paused":paused, "pid":state.get("pid"),
            "birth":state.get("birth"), "generation":state.get("generation"),
            "last_cycle":state.get("last_cycle"), "calendar_modified":False}


def cycle_wait_seconds(config, cycle):
    """Catch up quickly, but do not hot-loop an unavailable provider."""
    active = {row["id"] for row in config["accounts"] if row["enabled"] and row["id"].startswith("gmail-")}
    statuses = [value for account,value in cycle.get("accounts",{}).items() if account in active]
    progressing = any(value.get("state") == "backfill_pending" or
                      (value.get("state") == "needs_attention" and value.get("pending",0)>0 and value.get("fetched_this_step",0)>0)
                      for value in statuses)
    if progressing:
        return config.get("backfill_pause_seconds",2)
    failed_pending = any(value.get("state") == "needs_attention" and value.get("pending",0)>0 for value in statuses)
    if failed_pending:
        return config.get("backfill_retry_seconds",60)
    return config["interval_seconds"]


def cycle_delay(cycle, normal_interval):
    values=cycle.get("accounts",{}).values()
    if any(v.get("pending",0)>0 and v.get("state") in {"backfill_pending","needs_attention"} and v.get("reason") != "disk_space_blocked" for v in values):
        return 2
    if any(v.get("failed",0)>0 for v in cycle.get("accounts",{}).values()):
        return min(60,normal_interval)
    return normal_interval


def run_worker(path):
    import threading
    import uuid
    config = load_config(path)
    directory = Path(config["state_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "paused.json").exists():
        return {"state":"paused", "running":False}
    with NamedMutex(directory / "worker"):
        if (directory / "paused.json").exists():
            return {"state":"paused", "running":False}
        state = {"pid":os.getpid(), "birth":_birth(os.getpid()), "generation":uuid.uuid4().hex,
                 "heartbeat":_utc(), "state":"running", "last_cycle":None}
        if state["birth"] is None:
            raise RuntimeError("Worker process identity could not be verified")
        guard, finished, failed = threading.Lock(), threading.Event(), threading.Event()
        def publish():
            with guard:
                state["heartbeat"] = _utc()
                _write_json(directory / "worker.json", state)
        publish()
        def pulse():
            while not finished.wait(5):
                try:
                    publish()
                except Exception:
                    failed.set()
                    break
        heartbeat = threading.Thread(target=pulse, daemon=True)
        heartbeat.start()
        try:
            while not (directory / "paused.json").exists():
                cycle = run_once(path, respect_pause=True)
                with guard:
                    state["last_cycle"] = cycle
                publish()
                # Stop checks are frequent; retries remain bounded by the configured interval.
                deadline = __import__("time").monotonic() + cycle_wait_seconds(load_config(path), cycle)
                while __import__("time").monotonic() < deadline:
                    if failed.is_set():
                        raise RuntimeError("Heartbeat persistence failed")
                    if (directory / "paused.json").exists():
                        break
                    finished.wait(min(1, max(0, deadline - __import__("time").monotonic())))
        finally:
            finished.set()
            heartbeat.join(timeout=6)
            with guard:
                state["state"] = "stopped"
            publish()
    return {"state":"stopped", "running":False}


def _start_scheduled_worker(path):
    """Invoke only our matching task definition, without shell interpolation of paths."""
    env = _environment()
    from .launching import project_command
    command=project_command("mailorganizer.runtime",["run","--config",str(Path(path).resolve())],console=False)
    env["MAIL_ORGANIZER_CONFIG"] = str(Path(path).resolve())
    env["MAIL_ORGANIZER_PYTHONW"] = command[0]
    env["MAIL_ORGANIZER_ARGUMENTS"] = subprocess.list2cmdline(command[1:])
    script = """$ErrorActionPreference='Stop'; $t=Get-ScheduledTask -TaskName 'MailOrganizer.HiddenWorker' -TaskPath '\\' -ErrorAction SilentlyContinue; $a=$env:MAIL_ORGANIZER_ARGUMENTS; if($t -and $t.Actions.Execute -eq $env:MAIL_ORGANIZER_PYTHONW -and $t.Actions.Arguments -eq $a){Start-ScheduledTask -TaskName 'MailOrganizer.HiddenWorker' -TaskPath '\\'; 'TASK_STARTED'}else{'NOT_OWNED'}"""
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        return result.returncode == 0 and result.stdout.strip() == "TASK_STARTED"
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_worker(path):
    import time
    path = Path(path).resolve()
    config = load_config(path)
    directory = Path(config["state_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    previous = worker_status(path)
    if previous["running"]:
        return {"status":"already_running", **previous}
    (directory / "paused.json").unlink(missing_ok=True)
    from .launching import project_command
    command = project_command("mailorganizer.runtime", ["run", "--config", str(path)], console=False)
    via_task = _start_scheduled_worker(path)
    child = None if via_task else subprocess.Popen(command, env=_environment(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = worker_status(path)
        if result["running"] and result.get("heartbeat_fresh"):
            return {"status":"started", "launch_mode":"windows_task" if via_task else "direct_hidden", **result}
        if child is not None and child.poll() is not None and child.returncode != 0:
            raise RuntimeError("Hidden worker exited before readiness")
        time.sleep(0.1)
    raise RuntimeError("Hidden worker did not report readiness")


def stop_worker(path, wait_seconds=10):
    import time
    config = load_config(path)
    directory = Path(config["state_dir"])
    _write_json(directory / "paused.json", {"paused_at":_utc(), "reason":"operator_stop"})
    deadline = time.monotonic() + wait_seconds
    result = worker_status(path)
    while result["running"] and time.monotonic() < deadline:
        time.sleep(0.1)
        result = worker_status(path)
    return {"status":"stop_requested" if result["running"] else "stopped", **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hidden read-only worker controlled by commands/Hermes")
    parser.add_argument("command", choices=["init-config","run","once","status","start","stop"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "init-config":
            if args.archive_root is None or args.state_dir is None:
                raise ValueError("Explicit archive and state paths required")
            result = init_config(args.config, args.archive_root, args.state_dir)
        elif args.command == "status":
            result = worker_status(args.config)
        elif args.command == "start":
            result = start_worker(args.config)
        elif args.command == "stop":
            result = stop_worker(args.config)
        elif args.command == "run":
            try:
                result = run_worker(args.config)
            except WorkerBusy:
                result = {"status":"already_running"}
        else:
            config = load_config(args.config)
            with NamedMutex(Path(config["state_dir"]) / "worker"):
                result = run_once(args.config)
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        safe_error = {"status":"blocked", "error_type":type(error).__name__,
                      "native_error":getattr(error, "winerror", None), "calendar_modified":False}
        if args.command == "run":
            try:
                config = load_config(args.config)
                _write_json(Path(config["state_dir"]) / "worker_error.json", {**safe_error, "at":_utc()})
            except Exception:
                pass
        if sys.stdout is not None:
            print(json.dumps(safe_error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


