"""Local-only integrated document and evidence workflow preparation."""
from pathlib import Path
from .archive import Archive
from .enrichment import Enrichment
from .mail_analysis import analyze
from .workflows import build_workflows,export_workflows
from .cli import _write_json


def provider_metadata(root,records):
    by_account={}
    if (Path(root)/"gmail_sync.sqlite3").exists():
        from .gmail_sync import GmailSync
        for account in sorted({r["account_id"] for r in records if r["account_id"].startswith("gmail-")}):
            by_account[account]=GmailSync(root,account,None).message_metadata()
    return by_account


def prepare(root):
    root=Path(root).resolve()
    records=Archive(root).records()
    enrichment=Enrichment(root)
    enriched=enrichment.enrich_records(records)
    metadata=provider_metadata(root,enriched)
    for record in enriched:
        record["provider_metadata"]=metadata.get(record["account_id"],{}).get(record["message_id"],{})
    views=enrichment.export_views(enriched)
    analysis=analyze(enriched,provider_metadata=metadata)
    workflow=build_workflows(enriched)
    files=export_workflows(root,workflow)
    _write_json(root/"analysis.json",analysis)
    summary={"status":"local_pipeline_prepared","messages":len(records),"documents":sum(len(r.get("attachments",[])) for r in enriched),
             "actions":analysis["summaries"]["actions"],"cases":analysis["summaries"]["cases"],
             "calendar_proposals":len(analysis["calendar_proposals"]),"ics_proposals":len(workflow["calendar_proposals"]),
             "views":views,"files":files,"calendar_modified":False,"mailbox_modified":False,"sent_to_external_model":False}
    _write_json(root/"preparation_summary.json",summary)
    return summary
