# Pi dev profile

The Pi agent directory used in dev mode, both by `scripts/pi-dev` (standalone Pi CLI) and,
when `deploy/*.json` sets `"pi_profile": "agent-harness/profiles/dev"`, by the agent harness.
Only configuration is versioned here; Pi's runtime state (sessions, installed packages,
caches, credentials) is ignored by `.gitignore`.

| File | Purpose |
|---|---|
| `settings.json` | Startup defaults (`defaultProvider`, `defaultModel`, optional `defaultThinkingLevel`), pinned `packages`, extension paths |
| `extensions/` | Our own extensions |
| `skills/`, `prompts/`, `models.json` | Optional skills, prompt templates, custom endpoints |

Provider keys live outside the repository in `~/.config/balsamic/secrets.env`
(`DEEPSEEK_API_KEY=...`, mode 600); override with `BALSAMIC_SECRETS` or `secrets_file`.

## Workflow

1. Try something interactively: `scripts/pi-dev` (one-off extension: `scripts/pi-dev -e ./ext.ts`).
   Model and thinking level are runtime choices: `/model` (`Ctrl+L`), `/thinking` (`Shift+Tab`).
2. Keep it: `scripts/pi-dev install npm:<package>@<version>` writes a pinned entry to `settings.json`;
   put our own extensions in `extensions/`.
3. Commit the profile change, then restart the service so harness agents load it.

## What dev mode changes in the harness

- Profile extensions, packages and skills load; the application's instructions stay the system
  prompt, context files are not discovered, and Pi's built-in file/shell tools stay off.
- Agents' model and thinking level can be changed at runtime from **Models**, within the
  campaign's model family; a change applies at the agent's next turn.
- Without a profile (locked mode) none of this is configurable.

A campaign is bound to the model family it was activated with (for example `deepseek` or
`openai`). Switching models within that family is allowed; continuing a campaign with another
family is refused. Per-call tokens, cache use and cost are on the **LLM usage** page; API-billed
calls count against the campaign's LLM cap, subscription calls do not.
