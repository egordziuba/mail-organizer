# Gmail completion — live state

Source: this repository.

Scope: finish two Gmail accounts; Outlook deferred, configuration preserved and disabled for now. Mailbox access is read-only. No external model upload or calendar writes.

Confirmed baseline: two independent Gmail OAuth slots work; source profiles report 29,434 and 5,514 messages at discovery. Existing archive is only a pilot subset. Production worker paused for a safe upgrade, existing originals/tokens retained.

Implementation split: Gmail persistent catalog/history engine; offline document cache/views; conservative cases/actions/calendar proposals. Parent integrates commands, runtime, report and verifies live sources. Full archive must never be claimed from a per-step limit or resultSizeEstimate.

Verification: synthetic TDD + real catalog enumeration, raw-file checks, cursor resume and no refetch of known originals; operational hidden worker read-back. First full backfill is large and must survive session exit/reboot via persistent queue. Pending count and failure count remain visible until actual completion.
