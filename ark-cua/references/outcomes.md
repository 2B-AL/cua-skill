# Task results

Use `data.outcome` to decide what happens next. Keep the returned invocation/task identifier and follow `next.command` for the same task.

| Outcome | Action |
| --- | --- |
| `in_progress` | Run `next.command`; do not answer from progress text. |
| `needs_input` | Relay `data.input_request.question` verbatim and submit the user's own reply unchanged; never answer it yourself. |
| `completed` | Reply with `data.result.text` as returned; it is the authoritative final result. |
| `failed` | Explain the failure; retry only when requested and safe. |
| `cancelled` | Report cancellation. |

A timeout is not a task outcome. Continue the same invocation after a retryable connection error; never submit the task again or infer completion from progress text. `data.wait_interrupted` on a create or answer means the call took effect and only the wait after it failed: continue with `next.command`.

`ACTIVE_RUN_CONFLICT` means the new request was rejected before task creation; SKILL.md says when to release your own MCP session and when to stop.
