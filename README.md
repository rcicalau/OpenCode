# Code Buddy

Code Buddy is a Windows-first Python coding agent. It is built around safe file editing, policy-controlled command execution, local session state, and validation-driven workflows.

The product specification lives in [SPEC.md](SPEC.md).

## Development

Use Python 3.12 or newer:

```powershell
py -3.12 -m unittest discover -s tests
```

The Windows `python` command may point at an older interpreter. Prefer the Python launcher command above.

## Install The Buddy Command

Run this once from the Code Buddy repo:

```cmd
install-buddy.cmd
```

The installer installs Code Buddy in editable mode and pulls the terminal UI dependencies (`prompt_toolkit` and `rich`) for Python 3.12.

Open a new `cmd.exe` window, then start Code Buddy from anywhere:

```cmd
buddy
```

With no arguments, `buddy` starts chat mode, opens a native folder picker, remembers the last folder, and stores that project's state in:

```text
<project>\.pyagent\
```

You should not need to set `PYTHONPATH` manually.

## Terminal Experience

Interactive chat uses a colorized terminal UI:

- `Enter` sends the message.
- `Shift+Enter` inserts a newline in terminals that emit modified Enter key sequences.
- `Esc`, then `Enter`, inserts a newline as a reliable fallback.
- Multiline paste is inserted as-is.
- Slash commands autocomplete while typing `/`.
- Agent actions such as git branch creation, file edits, shell commands, searches, and validation are shown inline before the final answer.

Production deployment is configured for an Azure-authenticated OpenAI-compatible endpoint by default, using model `openai/gpt-5.4`.

Set the endpoint URL as a persistent Windows user environment variable:

```cmd
setx AZURE_OPENAI_BASE_URL "https://your-endpoint/openai/v1"
```

Close and reopen `cmd.exe`. Code Buddy then loads `auth:AzureAuthClient` from the selected project root, calls its `get_token()` method, and passes that token to the OpenAI Python SDK for every model request. The default provider config is:

```toml
[model.roles.main]
provider = "azure_openai"
model = "openai/gpt-5.4"

[model.providers.azure_openai]
base_url_env = "AZURE_OPENAI_BASE_URL"
auth_client = "auth:AzureAuthClient"
token_method = "get_token"
verify_ssl = false
```

For development and provider smoke testing with Perplexity, set `PERPLEXITY_API_KEY` and configure the main provider as `perplexity` in `<project>\.pyagent\config.toml`.

## Project Binding

Code Buddy stores project-local state under the selected project root:

```text
<project>\.pyagent\sessions\
<project>\.pyagent\index\
<project>\.pyagent\workplans\
<project>\.pyagent\logs\
```

On launch, Code Buddy refreshes a project map at:

```text
<project>\.pyagent\index\project_map.md
<project>\.pyagent\index\project_memory.json
```

That map includes the project shape, key docs/manifests, source symbols where available, and the active session's objective, plan, pending next step, inspected files, edited files, commands, and blockers. Future launches reuse the same project-local `.pyagent` state.

The index also writes deterministic module summaries:

```text
<project>\.pyagent\index\module_summaries.json
```

Large execution objectives such as "document each file in the codebase" are split into durable work-plan slices under `.pyagent\workplans`. Each slice is validated, recorded in status, and can be resumed with `continue` or retried with `retry blocked`.

When you start it inside a git repo, or a folder with `.pyagent\config.toml`, `pyproject.toml`, `SPEC.md`, or `AGENTS.md`, that folder becomes the project root. You can also bind it explicitly:

```cmd
buddy --root C:\path\to\project chat
```

Or for the current terminal:

```cmd
set CODEBUDDY_PROJECT_ROOT=C:\path\to\project
buddy chat
```

Each project gets its own `.pyagent` state, so opening Code Buddy in project A resumes project A, and opening it in project B resumes project B.

## Persistent API Keys

API keys and Azure tokens are intentionally not stored in project `.pyagent\config.toml`, because project files can be committed or shared. By default, Code Buddy uses your project-local `auth.py` file:

```python
class AzureAuthClient:
    def get_token(self):
        return "token-value"
```

The token method may also return an object with a `.token` attribute.

For dev/test with Perplexity, Code Buddy reads Perplexity from `PERPLEXITY_API_KEY`.

```cmd
setx PERPLEXITY_API_KEY "pplx-your-key-here"
```

`setx` persists the variable for future terminals. It does not update already-open terminals, so close and reopen `cmd.exe` before testing.

There is also a small helper that prompts for the key and calls `setx` for the provider configured in Code Buddy:

```cmd
buddy auth set perplexity
```

Check auth:

```cmd
buddy auth status perplexity
buddy auth check perplexity
buddy doctor
```

`auth status` checks whether Code Buddy can see the variable in the current process. `auth check` performs a small live provider call and distinguishes “variable missing” from “provider rejected the key.” If the provider returns HTTP 401 after `auth status` says the variable is set, regenerate the key, run `setx` again, and reopen the terminal.

## Agent Loop

Code Buddy sends native OpenAI-compatible tool schemas when available and also supports its text-tool fallback. For execution tasks, it loops over tool calls with a bounded iteration budget, feeding results back into the model until the model gives a final answer or the loop budget is reached.

Streaming transport support is implemented for OpenAI-compatible server-sent-event responses, including content deltas and native streamed tool-call reconstruction. The terminal renderer can display streamed assistant chunks. Tool-using turns may still choose non-streaming completion when that keeps the retry loop simpler and more deterministic.

After file edits, Code Buddy runs validation even if the model forgets to request it. Shell commands stay inside the selected project root, hard-deny commands cannot be bypassed by ordinary tool approval, and Git branch creation refuses dirty user work unless explicitly approved.

If Git branch creation stops because the worktree already has user changes, review the changes, then type `y`, `/a`, `/approve`, or `/approve-branch` to allow one agent-branch creation carrying those changes and continue the saved objective.
