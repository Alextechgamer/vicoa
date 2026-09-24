# Canary: shared skills, memory, context, handoff

Run: 2026-09-24T02:44:11Z. Disposable local database only. No live Agent Control service, agy-1, agy-2, task 6, or jobs 70/71 were contacted. No worker was launched.

Result: pass

| Check | Result | Detail |
| --- | --- | --- |
| skill waits for canary | pass | canary_required |
| skill activates after canary | pass | active |
| untrusted memory does not override owner | pass | rejected |
| routing explains skill memory and context | pass | account,context,memory,provider,quota,skill |
| pressure does not kill a healthy session | pass | kept=True status=running |
| context stays inside budget and drops raw logs | pass | used=79 omitted=5 |
| handoff packet is complete and redacted | pass | prepared |
| resume does not launch or reuse the session | pass | canary-session-2 |
| old session cannot restart | pass | session_reused |
| secrets and live homes stay out of views | pass | redacted |
