"""Command-line interface: local processing and explicitly authorized read-only sync."""
import argparse
import json
from pathlib import Path


def build_parser():
    parser = argparse.ArgumentParser(description="Read-only Mail Organizer: два Gmail и один Outlook. Запись в календарь отсутствует.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["demo", "report", "prepare", "tasks", "validate-results", "import-eml", "sync", "auth", "status"]:
        sub = commands.add_parser(name)
        if name not in {"auth", "status"}:
            sub.add_argument("--root", type=Path, required=True, help="Локальная папка архива; demo должен быть отдельным")
        if name == "import-eml":
            sub.add_argument("--input", type=Path, required=True)
            sub.add_argument("--account", required=True)
        if name == "report":
            sub.add_argument("--output", type=Path)
        if name == "tasks":
            sub.add_argument("--output", type=Path)
        if name == "validate-results":
            sub.add_argument("--input", type=Path, required=True)
        if name in {"auth", "status", "sync"}:
            sub.add_argument("--account", choices=["gmail-1", "gmail-2", "outlook-1"], required=True)
            sub.add_argument("--client-id", help="Публичный Application client ID для Microsoft OAuth")
        if name == "auth":
            sub.add_argument("--client-file", type=Path, help="Путь к Google Desktop OAuth JSON; не вставляйте содержимое в чат")
        if name == "sync":
            sub.add_argument("--query", default="", help="Gmail search query; пустой = без фильтра периода")
            sub.add_argument("--folder", default="inbox", help="Graph folder; по умолчанию только Inbox")
            sub.add_argument("--max-messages", type=int, default=100, help="Предохранительный предел, НЕ полная выгрузка")
            sub.add_argument("--gmail-mode", choices=["pilot","incremental"], default="pilot")
            sub.add_argument("--include-spam-trash", action="store_true")
            sub.add_argument("--time-budget-seconds", type=int, default=120)
            sub.add_argument("--workers", type=int, default=4)
    return parser


def _write_json(path, value):
    import os
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".mailorganizer-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers may briefly deny delete-sharing; retry only this atomic commit.
        import time
        deadline = time.monotonic() + 2
        while True:
            try:
                Path(temporary).replace(path)
                break
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _dataset(root, kind=None):
    path = root / "dataset.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"kind":"imported"}
    if kind is not None:
        if kind == "synthetic" and not path.exists() and root.exists() and any(root.iterdir()):
            raise ValueError("Refusing to put a demo inside unlabelled existing data")
        if path.exists() and previous.get("kind") != kind:
            raise ValueError("Synthetic and real/imported archives must be separate")
        previous = {"kind":kind}
        _write_json(path, previous)
    return previous


def _safe_export(root, output, suffix):
    root, output = root.resolve(), output.resolve()
    if output.suffix.lower() != suffix or output.is_relative_to(root / "messages"):
        raise ValueError("Export cannot overwrite archived sources; use the correct export extension")
    return output


def _report(root, output=None):
    from .archive import Archive
    from .reporting import render_report, summarize
    if not root.exists():
        raise ValueError("Archive root does not exist")
    records = Archive(root).records()
    kind = _dataset(root).get("kind")
    label = "SYNTHETIC TEST DATA — не ваша почта" if kind == "synthetic" else "Локально сохранённые письма"
    coverage = "Только искусственные тестовые письма; аккаунты не подключены." if kind == "synthetic" else "Только импортированные/загруженные письма; полный серверный архив не подтверждён."
    proposals_path = root / "model_proposals.json"
    proposals = json.loads(proposals_path.read_text(encoding="utf-8")) if proposals_path.exists() else []
    output = _safe_export(root, output or root / "report.html", ".html")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_report(records, label=label, coverage=coverage, proposals=proposals), encoding="utf-8")
    summary = {**summarize(records), "data_kind":kind, "report":str(output.resolve()), "calendar_modified":False}
    _write_json(root / "summary.json", summary)
    return summary


def _demo(root):
    from email.message import EmailMessage
    from .archive import Archive
    _dataset(root, "synthetic")
    archive = Archive(root)
    samples = [
        ("gmail-1", "Bewerbung Eingangsbestätigung", "Vielen Dank für Ihre Bewerbung. Wir haben Ihre Bewerbung erhalten.", "hr@example.test"),
        ("gmail-1", "Einladung zum Vorstellungsgespräch", "Wir laden Sie zum Vorstellungsgespräch am 20.10.2026 um 14:00 ein. Bitte bestätigen Sie den Termin.", "hr@example.test"),
        ("gmail-2", "Hochschule: fehlende Unterlagen", "Für Ihre Immatrikulation benötigen wir die fehlenden Unterlagen. Bitte reichen Sie die Dokumente bis zum 25.10.2026 ein.", "admissions@university.example.test"),
        ("gmail-2", "Anmeldebestätigung", "Ihre Anmeldung wurde bestätigt. Keine weitere Aktion ist erforderlich.", "service@example.test"),
        ("outlook-1", "Rechnung: Zahlungsfrist", "Bitte zahlen Sie die Rechnung bis zum 22.10.2026. Dies ist eine künstliche Testrechnung, keine Zahlungsaufforderung.", "billing@example.test"),
        ("outlook-1", "Offene Rückfrage", "Wir melden uns vielleicht nächste Woche. Weitere Informationen folgen.", "office@example.test"),
    ]
    for i, (account, subject, body, sender) in enumerate(samples, 1):
        message = EmailMessage()
        message["Subject"] = "SYNTHETIC — " + subject
        message["From"] = sender
        message["To"] = "synthetic-user@example.test"
        message["Date"] = "Thu, 01 Oct 2026 12:00:00 +0200"
        message["Message-ID"] = f"<synthetic-{i}@example.test>"
        message.set_content("SYNTHETIC TEST DATA — НЕ РЕАЛЬНАЯ ПОЧТА.\n" + body)
        if i == 3:
            message.add_attachment("SYNTHETIC: List of required documents.\nPassport copy, application form.\n".encode(), maintype="text", subtype="plain", filename="synthetic-document-list.txt")
            message.set_boundary(f"synthetic-mail-organizer-{i}")
        archive.ingest(account, f"synthetic-{i}", message.as_bytes())
    return {"status":"demo_verified", **_report(root)}


def _model_tasks(root, output=None):
    from .archive import Archive
    from .model_contract import make_task
    if not root.exists():
        raise ValueError("Archive root does not exist")
    tasks, skipped = [], []
    for record in Archive(root).records():
        try:
            tasks.append(make_task(record))
        except ValueError:
            skipped.append({"message_key":record["message_key"], "reason":"Security/oversized input excluded; needs local review"})
    output = _safe_export(root, output or root / "model_tasks.jsonl", ".jsonl")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")
    _write_json(root / "model_task_index.json", {t["task_id"]:t for t in tasks})
    _write_json(root / "model_task_review.json", skipped)
    return {"status":"tasks_exported_locally", "exported":len(tasks), "skipped_for_review":len(skipped), "path":str(output.resolve()), "sent_to_external_model":False}


def _validate_model_batch(root, input_path):
    from .archive import Archive
    from .model_contract import make_task, validate_result
    if input_path.stat().st_size > 5_000_000:
        raise ValueError("Model response batch exceeds the local bound")
    index = json.loads((root / "model_task_index.json").read_text(encoding="utf-8"))
    content = input_path.read_text(encoding="utf-8")
    outputs = json.loads(content) if content.lstrip().startswith("[") else [json.loads(line) for line in content.splitlines() if line.strip()]
    if not isinstance(outputs, list) or not outputs or len(outputs) > 1000:
        raise ValueError("Model response batch must be a bounded nonempty list")
    archive, validated, seen = Archive(root), [], set()
    for output in outputs:
        if not isinstance(output, dict) or not isinstance(output.get("task_id"), str):
            raise ValueError("Invalid model response identity")
        task_id = output["task_id"]
        if task_id in seen or task_id not in index:
            raise ValueError("Unknown or duplicate task identity")
        seen.add(task_id)
        task = index[task_id]
        source = archive.get(task["message_key"])
        if source is None or make_task(source)["task_id"] != task_id:
            raise ValueError("Source changed after task export")
        validated.append(validate_result(make_task(source), output))
    destination = root / "model_proposals.json"
    previous = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else []
    merged = {p["task_id"]:p for p in previous}
    merged.update({p["task_id"]:p for p in validated})
    _write_json(destination, list(merged.values()))
    return {"status":"proposals_validated_not_approved", "count":len(validated), "calendar_modified":False, "mailbox_modified":False}


def _sync_gmail(args):
    from .auth import google_access_token
    from .connectors import GmailReader
    from .gmail_sync import GmailSync
    _dataset(args.root, "imported")
    engine = GmailSync(args.root, args.account, GmailReader(google_access_token(args.account)),
                       include_spam_trash=args.include_spam_trash, query=args.query)
    result = engine.step(max_messages=args.max_messages, max_pages=20,
                         time_budget_seconds=args.time_budget_seconds, workers=args.workers)
    status_file = args.root / "sync_status.json"
    statuses = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
    statuses[args.account] = {**result, "provider":"gmail", "mailbox_modified":False}
    _write_json(status_file, statuses)
    return {**result, "account_id":args.account, "mailbox_modified":False, "calendar_modified":False}


def _sync(args):
    if args.account.startswith("gmail-") and args.gmail_mode == "incremental":
        return _sync_gmail(args)
    import hashlib
    from . import auth, connectors
    from .archive import Archive
    if args.max_messages <= 0:
        raise ValueError("A positive message cap is required")
    provider = "outlook" if args.account == "outlook-1" else "gmail"
    if provider == "gmail":
        reader = connectors.GmailReader(auth.google_access_token(args.account))
    else:
        if not args.client_id:
            raise ValueError("Microsoft public application client ID required")
        reader = connectors.GraphReader(auth.microsoft_access_token(args.account, args.client_id))
    profile = reader.get_profile()
    identity = profile.get("emailAddress") if provider == "gmail" else profile.get("id")
    if not isinstance(identity, str) or not identity:
        raise ValueError("Mailbox identity could not be verified")
    identity_hash = hashlib.sha256((provider + ":" + identity.casefold()).encode()).hexdigest()
    _dataset(args.root, "imported")
    registry_path = args.root / "account_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {}
    if any(k != args.account and v["identity_sha256"] == identity_hash for k,v in registry.items()):
        raise ValueError("Same mailbox cannot be counted as two account sources")
    if args.account in registry and registry[args.account]["identity_sha256"] != identity_hash:
        raise ValueError("Account slot identity changed; choose a new archive/slot")
    registry[args.account] = {"provider":provider, "identity_sha256":identity_hash, "identity_verified":True}
    _write_json(registry_path, registry)
    archive = Archive(args.root)
    status_path = args.root / "sync_status.json"
    statuses = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    from datetime import datetime, timezone
    status = {"provider":provider, "scope":args.query if provider == "gmail" else args.folder,
              "max_messages":args.max_messages, "downloaded":0, "inserted":0,
              "full_mailbox_verified":False, "state":"running", "mailbox_modified":False,
              "started_at":datetime.now(timezone.utc).isoformat()}
    statuses[args.account] = status
    _write_json(status_path, statuses)
    messages = reader.iter_messages(query=args.query, max_messages=args.max_messages) if provider == "gmail" else reader.iter_messages(folder=args.folder, max_messages=args.max_messages)
    try:
        for message_id, raw in messages:
            record = archive.ingest(args.account, message_id, raw)
            status["downloaded"] += 1
            status["inserted"] += bool(record["inserted"])
            _write_json(status_path, statuses)
    except Exception:
        status["state"] = "failed_partial"
        _write_json(status_path, statuses)
        raise
    status.update(state="bounded_scan_finished", finished_at=datetime.now(timezone.utc).isoformat())
    _write_json(status_path, statuses)
    return {"status":"bounded_readonly_scan_finished", "account_id":args.account, **status, "calendar_modified":False}


def _archive_lock(identity):
    """Real archive lock; synthetic tests may explicitly replace this factory."""
    from .runtime import NamedMutex
    return NamedMutex(identity)


def _dispatch(args):
    # Archive construction can replay complete orphan originals into SQLite, even on report.
    if args.command in {"sync", "demo", "import-eml", "report", "prepare", "tasks", "validate-results"}:
        with _archive_lock(args.root.resolve() / "archive-writer"):
            return _dispatch_unlocked(args)
    return _dispatch_unlocked(args)


def _dispatch_unlocked(args):
    if args.command == "prepare":
        from .processing import prepare
        return prepare(args.root)
    if args.command == "sync":
        return _sync(args)
    if args.command in {"auth", "status"}:
        from . import auth
        provider = "outlook" if args.account == "outlook-1" else "gmail"
        if args.command == "status":
            return auth.auth_status(args.account, provider)
        if provider == "gmail":
            if not args.client_file or not args.client_file.is_file():
                raise ValueError("Google Desktop OAuth client file required")
            return auth.google_authorize(args.account, args.client_file)
        if not args.client_id:
            raise ValueError("Microsoft public application client ID required")
        return auth.microsoft_authorize(args.account, args.client_id)

    if args.command == "tasks":
        return _model_tasks(args.root, args.output)
    if args.command == "validate-results":
        return _validate_model_batch(args.root, args.input)
    if args.command == "demo":
        return _demo(args.root)
    if args.command == "import-eml":
        import hashlib
        from .archive import Archive
        if not args.input.is_file() or args.input.stat().st_size > 50_000_000:
            raise ValueError("Input must be a bounded EML file")
        raw = args.input.read_bytes()
        _dataset(args.root, "imported")
        record = Archive(args.root).ingest(args.account, hashlib.sha256(raw).hexdigest(), raw)
        return {"status":"imported", "message_key":record["message_key"], "inserted":record["inserted"], "mailbox_modified":False}
    if args.command == "report":
        return _report(args.root, args.output)
    raise ValueError("Command implementation is not available")


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = _dispatch(args)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"status":"blocked", "error_type":type(exc).__name__, "reason":getattr(exc, "reason", "needs_attention"), "mailbox_modified":False, "calendar_modified":False}))
        return 2



if __name__ == "__main__":
    raise SystemExit(main())
