# ARK CUA

This repository publishes the **ark-cua** skill, which lets a coding agent such
as Claude Code or Codex do work on the user's cloud computer through Volcengine
AgentPlan CUA: delegate tasks and follow-ups, take screenshots, bring files
back, and manage desktop access. The repository is `2B-AL/cua-skill`; the skill
directory and name are both `ark-cua`.

## Install or update

```bash
npx -y skills add 2B-AL/cua-skill --skill ark-cua -g
```

Choose the coding agents to install to. For a non-interactive installation to
Codex and Claude Code:

```bash
npx -y skills add 2B-AL/cua-skill --skill ark-cua --agent codex claude-code -g --copy -y
```

In Codex, invoke the installed skill as `$ark-cua`. If an earlier distribution installed
it as `byted-util-ark-cua`, remove it through the installer after installing
`ark-cua`. This also removes the old update registration, which can otherwise
restore the previous skill name on a later update. Select the same agents as
the previous installation, for example:

```bash
npx -y skills remove byted-util-ark-cua --agent codex claude-code -g -y
```

Keep the runtime state directory described below.

Existing Git clones should use the current repository address:

```bash
git remote set-url origin https://github.com/2B-AL/cua-skill.git
```

## AgentPlan and development gateways

The bundled gateway is AgentPlan. Start with:

```bash
python3 <skill-dir>/scripts/cua.py auth status
```

The CLI reuses a saved login for that gateway or an eligible personal AgentPlan
Max account from `arkcli`. A development gateway uses its own API key or
environment token:

```bash
python3 <skill-dir>/scripts/cua.py --api-base-url "<DEV_GATEWAY_URL>" auth login
```

Enter the credential in the terminal's hidden prompt. The selected gateway is
saved with the login and used by later commands. A saved credential is sent
only to its gateway; `arkcli` credentials are used only for the bundled
AgentPlan gateway. See [authentication](ark-cua/references/auth.md) for account
switching and gateway selection.

The login and session are kept in `~/.ark-cua/` (`auth.json` and
`session.json`). Earlier releases used `~/.openclaw/ark-cua/`; on its first run
the current CLI moves those two files and leaves everything else under
`~/.openclaw` alone. Both `AP_CUA_SKILL_*` and `CUA_SKILL_*` overrides remain
supported for the API base URL, auth file and session file; with an auth or
session file override set, nothing is moved. One login is saved per auth file;
logging in to another gateway replaces that login. `session.json` also keeps
the creation request of each unfinished task, objective included, so that a
task can be resumed after CUA loses model authorization; the entry is removed
when the task ends.

Available operations depend on the selected gateway's capabilities and
authorization. Credential Agent resource sync (environment variables, Secrets,
Credential Sets and managed files) always uses the production Credential Agent
service with AgentPlan device sign-in, whichever CUA gateway is selected. See
[commands](ark-cua/references/commands.md) and
[credential resources](ark-cua/references/credentials.md).

## Local checks

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ark-cua/tests
node --test ark-cua/tests/cookiecloud-extension.test.mjs
```
