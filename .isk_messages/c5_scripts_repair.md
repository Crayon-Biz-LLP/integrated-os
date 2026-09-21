chore(scripts): read-only task-message diagnostics + one-shot repair for the Sep 14 rental-task close (approved)

Read-only diagnostics for the "move my rental agreement signing task to
tomorrow" incident — diag_latest_task_message.py shows the latest inbound
non-system user message and matching created tasks (due + Google sync) per
the sync rule; diag_rental_agreement_message.py walks the "sign the rental
agreement" messages and what became of them. Neither writes.

repair_rental_task_5966.py is the explicitly approved one-shot Sep 14 repair:
reopens task #5966 (closed by the bare-ack completion incident) via the OS's
own update_task_status('todo', reminder_at=…) so Google Task + the calendar
event resync, then neutralizes the completion-memory + dropped-button
clarification artifacts the erroneous closure produced (direct deletes ONLY
for those two wrong artifacts; the follow-up reminder restore to its original
pre-incident Sep 14 5pm IST slot uses the executor's reschedule semantics —
update_task_status alone cannot resync an already-open task due to the
no-change guard).

Root Cause: Sep 14 incident — bare "Yes." escaped a dropped-button
confirmation to the classifier as completion, closing task #5966 + deleting
its calendar event; the repair marks the workflow kept-queue so the standby
resume path is honest.
