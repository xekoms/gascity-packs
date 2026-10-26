# Claude coding routes

One **Claude Code** workflow provides both aliases with the same capabilities.

| Invocation | Model authentication and billing |
| --- | --- |
| `@claude <task>` | Existing Claude subscription OAuth |
| `@claude-api <task>` | Anthropic API key and that key's Anthropic workspace credits |

GitHub Actions runner charges are separate. Model names and custom agent files do not change billing.

## Use either alias

- Add the alias and task to a new issue/PR conversation comment, inline review comment, or submitted review.
- Include the alias in an issue title or body when opening it. The owner assigning an issue containing the alias supplies its title/body as the task, regardless of its author or assignee.
- For a review, comment `@claude review this PR` or `@claude-api review this PR`.
- For manual work, open Actions → **Claude Code** → **Run workflow**, select the branch and **subscription** or **api**, then enter a prompt. Include the issue/PR URL for discussion context. Manual dispatch checks out the selected branch, not automatically the PR head.

Owner authorization is required. If both aliases appear in the triggering comment/review, or across an issue's title and body, **API wins** and only one action runs. The unused credential input is empty. Manual and assignment runs use upstream agent mode with an explicit prompt, so they do not receive mention mode's automatic tracking comments or branch setup. Both billing choices behave alike within each mode. Missing credentials fail instead of falling back to the other billing route. One classifier uses upstream word-boundary and case-insensitive matching for both admission and billing. Suffixes such as `@claude-api-notes` are not triggers.

The manual button becomes discoverable after the workflow reaches the default branch. A prepared branch does not activate the default-branch workflow.

Existing automatic reviews and schedules keep their existing authentication and cadence. Either alias can request similar work on demand; this change does not duplicate automatic paid runs. Edited comments, labels, repository dispatch, and new schedules are not configured.

## Credentials and surface

The API route expects the repository Actions secret `ANTHROPIC_API_KEY`; the subscription route retains its existing OAuth secret. Do not put secrets in files, prompts, comments, URLs, or logs. Credential provisioning, App permission changes, and a bounded paid smoke test need separate approval.

These are GitHub Actions routes in GitHub discussions and the Actions manual form. They do not add an Anthropic-billed option to Copilot's native agent picker. App access, secret availability, and runner readiness must be verified before activation.

Offline routing checks: `node --test .github/claude-routing.test.mjs`. These checks do not invoke Claude or read credentials.

Runner selected for this new interactive workflow: `blacksmith-2vcpu-ubuntu-2404`. Existing workflows are unchanged. Credential/App availability and runner readiness are activation prerequisites, not established by branch preparation.

Exact routing finishes before the action job enters shared concurrency. Unrelated events do not replace pending requests. GitHub still retains at most one pending accepted invocation per group; a later accepted request may replace it.
