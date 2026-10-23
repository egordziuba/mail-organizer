# Mail Organizer

[![Tests](https://github.com/egordziuba/mail-organizer/actions/workflows/tests.yml/badge.svg)](https://github.com/egordziuba/mail-organizer/actions/workflows/tests.yml)

A local, **read-only** mail archive and triage pipeline for several mailboxes (two Gmail accounts and one Outlook account). It pulls messages, stores the originals immutably, sorts them by topic and required action, and builds an offline HTML report. It never sends, moves, deletes or relabels mail and never writes to a calendar.

Documentation inside the project is in Russian; the code and CLI are in English.

## Features

- **Immutable archive.** Original `.eml`, extracted text and attachments are stored with SHA-256 hashes and a SQLite index; duplicates and ID conflicts between accounts are detected.
- **GET-only connectors** for the Gmail API and Microsoft Graph with pagination limits, bounded retries and protection against sending the bearer token to a foreign host or through a redirect.
- **Separate OAuth per mailbox**, tokens kept in the Windows Credential Vault (via `keyring`); the browser opens only on an explicit `auth` command.
- **Incremental and resumable Gmail sync**, background worker started by Windows Task Scheduler with a single-instance lock and per-account error isolation.
- **Conservative rule-based triage** in German, English, Russian and Ukrainian: topic (`jobs`, `university`, `finance`, `housing`, …), observed sub-type, required action and a review queue.
- **Language-model hand-off as proposals, not actions.** Narrow tasks are exported for a model; answers are imported only after schema, task-ID, source-freshness, quote and date-expression checks, and a batch with errors is not applied partially.
- **Offline HTML report** with filters, search, provenance and documents.

![Synthetic triage list with category filters and search](docs/img/demo-triage.png)
![Synthetic message detail with source evidence and an attachment](docs/img/demo-provenance.png)

Synthetic demo: triage filters and message provenance; all addresses use `example.test`.

## Quick start (synthetic data)

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -e '.[auth,test]'
.venv/Scripts/python.exe -m mailorganizer.cli demo --root demo-data
# open demo-data/report.html — marked "SYNTHETIC TEST DATA"
```

Real mailboxes: copy `runtime.example.json` to `runtime.json`, then run `mailorganizer.cli auth --account gmail-1 --client-file <google-desktop-client.json>` and `sync`. See [README.ru.md](README.ru.md) and [RUNBOOK.md](RUNBOOK.md).

## Tests

`python -E -X utf8 -m pytest -q` — 297 tests: 287 passed, 10 skipped on Linux. The project targets Windows (Credential Vault, Task Scheduler, process supervision); Windows-only tests are skipped on other operating systems.

## Privacy

`real-mail/`, `.runtime/`, `runtime.json`, tokens and client secrets are git-ignored. All test and demo data is synthetic (`example.test` addresses).
