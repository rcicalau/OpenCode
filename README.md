# Code Buddy

Code Buddy is a Windows-first Python coding agent. It is built around safe file editing, policy-controlled command execution, local session state, and validation-driven workflows.

The product specification lives in [SPEC.md](SPEC.md).

## Development

Use Python 3.12 or newer:

```powershell
python -m unittest discover -s tests
```

The `python` command must point at Python 3.12 or newer.

## Run Without Installing

From any project folder, call the repo launcher directly:

```cmd
C:\Users\RaduC\Documents\OpenCode\run-buddy.cmd
```

With no arguments, it starts chat bound to the project you launched it from. With arguments, it runs a one-shot prompt using that same project root:

```cmd
C:\Users\RaduC\Documents\OpenCode\run-buddy.cmd "what does this project do?"
```

The launcher sets `PYTHONPATH` for this process only. It does not install Code Buddy globally and does not change your PATH. If `C:\Users\RaduC\Documents\OpenCode\.venv\Scripts\python.exe` exists, it uses that Python; otherwise it uses `python`.

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

With no arguments, `buddy` starts chat mode bound to the folder you launched it from, and stores that project's state in:

```text
<project>\.buddy\
```

You should not need to set `PYTHONPATH` manually.

To uninstall the global `buddy` command:

```cmd
buddy-uninstall.cmd
```

You can also run:

```cmd
buddy-install.cmd uninstall
```

The uninstaller removes the WindowsApps launcher that points to this checkout and attempts to uninstall the editable Python package. It does not remove project-local `.buddy` folders.

## Terminal Experience

Interactive chat uses a colorized terminal UI:

- `Enter` sends the message.
- `Shift+Enter` inserts a newline in terminals that emit modified Enter key sequences.
- `Esc`, then `Enter`, inserts a newline as a reliable fallback.
- Multiline paste is inserted as-is.
- Slash commands autocomplete while typing `/`.
- Project skills in `.buddy\skills\*.md` are callable as `/skill-name your request`.
- Agent actions such as git branch creation, file edits, shell commands, searches, and validation are shown inline before the final answer.

Production deployment is configured for an Azure-authenticated OpenAI-compatible endpoint by default, using model `openai/gpt-5.4`.

The repo includes a project config template:

```text
examples\project_config.azure_openai.toml
```

Code Buddy assumes your target project or `PYTHONPATH` provides `ai_mart.py` and `azure_auth.py`. The default Azure provider imports the endpoint from `ai_mart:base_url` and loads `azure_auth:AzureAuthClient`. The expected token method is `get_token()`, which can return either a string or an object with `.access_token`. The default provider config is:

```toml
[model.roles.main]
provider = "azure_openai"
model = "openai/gpt-5.4"

[model.providers.azure_openai]
base_url_import = "ai_mart:base_url"
auth_client = "azure_auth:AzureAuthClient"
token_method = "get_token"
verify_ssl = false
```

For development and provider smoke testing with Perplexity, set `PERPLEXITY_API_KEY` and configure the main provider as `perplexity` in `<project>\.buddy\config.toml`.

## Project Binding

Code Buddy stores project-local state under the selected project root:

```text
<project>\.buddy\sessions\
<project>\.buddy\index\
<project>\.buddy\workplans\
<project>\.buddy\logs\
```

Each active session stores durable conversation history:

```text
<project>\.buddy\sessions\<session-id>\conversation.jsonl
<project>\.buddy\sessions\<session-id>\ledger.json
<project>\.buddy\sessions\<session-id>\journal.jsonl
<project>\.buddy\sessions\<session-id>\compacted_state.md
```

`conversation.jsonl` records each turn's user prompt, assistant response, visible tool/model events, changed files, mode, and timestamp. `/compact` summarizes both the transcript and the ledger into `compacted_state.md`. Future model calls include the compacted memory when it exists, or recent conversation turns when it does not.

Compaction respects `storage.compact_max_tokens`, so generated memory stays under a bounded approximate token budget instead of growing without limit.

On launch, Code Buddy refreshes a project map at:

```text
<project>\.buddy\index\project_map.md
<project>\.buddy\index\project_memory.json
```

That map includes the project shape, key docs/manifests, source symbols where available, and the active session's objective, plan, pending next step, inspected files, edited files, commands, and blockers. Future launches reuse the same project-local `.buddy` state.

The index also writes deterministic module summaries:

```text
<project>\.buddy\index\module_summaries.json
```

Large execution objectives such as "document each file in the codebase" are split into durable work-plan slices under `.buddy\workplans`. Each slice is validated, recorded in status, and can be resumed with `continue` or retried with `retry blocked`.

When chat starts with unfinished work in the project, Code Buddy shows a short resume summary and asks whether to continue or start fresh. Starting fresh opens a new session and clears the active work-plan pointer.

When you start it from a terminal, the terminal's current folder is the project root. If you start inside a subfolder of an already configured Buddy project, Code Buddy reuses the nearest parent with `BUDDY.md` or `.buddy` state. You can also bind it explicitly for a one-off launch:

```cmd
buddy --root C:\path\to\project chat
```

Each project gets its own `.buddy` state, so opening Code Buddy in project A resumes project A, and opening it in project B resumes project B.

## Persistent API Keys

API keys and Azure tokens are intentionally not stored in project `.buddy\config.toml`, because project files can be committed or shared. By default, Code Buddy expects this project-local or importable auth shape:

```python
base_url = "https://your-endpoint/openai/v1"

class AiMartAuthClient:
    def authenticate_broker(self):
        return broker_token_object

class AzureAuthClient:
    def get_token(self):
        return AiMartAuthClient().authenticate_broker().access_token
```

`broker_token_object` must expose `.access_token`. `base_url` must be the OpenAI-compatible endpoint.

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

`auth status` checks whether Code Buddy can see the variable in the current process. `auth check` performs a small live provider call and distinguishes "variable missing" from "provider rejected the key." If the provider returns HTTP 401 after `auth status` says the variable is set, regenerate the key, run `setx` again, and reopen the terminal.

## Agent Loop

Code Buddy sends native OpenAI-compatible tool schemas when available and also supports its text-tool fallback. For execution tasks, it loops over tool calls with a bounded iteration budget, feeding results back into the model until the model gives a final answer or the loop budget is reached.

Streaming transport support is implemented for OpenAI-compatible server-sent-event responses, including content deltas and native streamed tool-call reconstruction. The terminal renderer can display streamed assistant chunks. Tool-using turns may still choose non-streaming completion when that keeps the retry loop simpler and more deterministic.

After file edits, Code Buddy runs validation even if the model forgets to request it. Python file edits are syntax-checked before write, so invalid Python is rejected without touching the file. Shell commands stay inside the selected project root, hard-deny commands cannot be bypassed by ordinary tool approval, and Git branch creation refuses dirty user work unless explicitly approved.

If Git branch creation or a shell command stops for approval, type `y`, `/a`, `/approve`, or `/approve-branch` to approve once and continue the saved objective. `/yolo` turns YOLO mode on and also approves any currently pending request once. Git remote detection reads the current project's `.git` remote config and recognizes both GitHub and GitLab-style origins, including self-hosted GitLab hosts.
