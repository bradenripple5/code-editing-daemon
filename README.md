# Code Editing Daemon

This extends the exact-text patch workflow in `direct_patch_console_raw_output.html` to multiple UTF-8 source files. The original browser console remains usable. A separate local Python process applies instructions supplied by a running Codex agent. No model, API key, or third-party dependency is required by the worker.

## Start on Windows

Run from this directory (Python 3.11+):

```powershell
$worker = Start-Process -FilePath (Get-Command python).Source -ArgumentList @('patch_daemon.py', 'serve', '--root', '.') -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -PassThru
```

This is a background process, not an installed Windows service; it does not automatically start at login. It binds only to loopback on an automatically selected port. `.patch-daemon/connection.json` contains the port, bearer token, PID, and workspace. Keep this file private to your OS account. Any process with its credentials has the worker's editing authority.

## Give it instructions from Codex

Tell the running agent:

> Use the patch daemon in this repository for edits. Write a JSON batch, submit a dry run, inspect its result, and then submit with dry_run false. Include the original files' SHA-256 hashes to detect stale edits.

The agent can use its shell tool to run:

```powershell
python patch_daemon.py submit request.json
```

Example `request.json` (paths are relative to the daemon's workspace):

```json
{
  "dry_run": true,
  "files": [
    {
      "path": "index.html",
      "operations": [
        {"type": "REPLACE", "target": "<title>Old</title>", "value": "<title>New</title>"}
      ]
    },
    {
      "path": "app.js",
      "operations": [
        {"type": "INSERT_AFTER", "target": "// setup", "value": "\ninitialize();"}
      ]
    }
  ]
}
```

Each file accepts optional `sha256` and either `operations` or `directions`. The latter is a string containing the original console's REPLACE / INSERT_BEFORE / INSERT_AFTER / REMOVE blocks. Every target must occur exactly once. Include any required insertion newlines explicitly. The JSON response reports changed files and before/after hashes. Omitted `dry_run` means true; set it to false to write changes. Requests are synchronous and serialized by the worker. Codex needs shell/network access to this local process under its existing permissions; this does not inject a new tool into an already running session.

## Guarantees and limits

All patches are validated before any writes. Each individual file is replaced atomically; ordinary write failures trigger best-effort rollback of preceding writes. A multi-file batch is **not crash-atomic**. External programs are not locked out: hashes and pre-write checks catch many conflicting edits, but an external write can still race the worker. Avoid having Codex and the worker edit the same files concurrently.

Existing UTF-8 files only; no create/delete/rename or shell execution. Bytes outside edits, including line endings, are retained. Paths must resolve inside the workspace; `.git`, `.codex`, `.agents`, `.aws`, and `.patch-daemon` paths are blocked. This is intended for a trusted local workspace, not hostile users who can concurrently change symlinks or filesystem paths. Credentials follow the containing directory's OS permissions. There is no persistent job queue or automatic recovery after a process crash.

Stop the process you started:

```powershell
Stop-Process -Id $worker.Id
Remove-Item -LiteralPath .patch-daemon/connection.json
```

Forced termination can leave the connection file behind. Remove it only after confirming its recorded process has stopped; the daemon refuses to overwrite an existing connection file.

Run verification with `python -m unittest -v`.

A future MCP adapter can expose the same worker as named Codex tools. Codex supports configured MCP connections: [official OpenAI documentation](https://developers.openai.com/learn/docs-mcp). The current shell client works without changing Codex configuration.
