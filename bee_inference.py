"""Subscription-backed, tool-free inference for untrusted transcript content."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def infer(system_prompt: str, user_message: str, timeout_s: int = 180) -> str:
    binary = shutil.which('claude') or next((str(path) for path in [Path.home() / '.local/bin/claude', Path.home() / '.npm-global/bin/claude', Path('/opt/homebrew/bin/claude'), Path('/usr/local/bin/claude')] if path.is_file()), None)
    if not binary:
        raise RuntimeError('Claude subscription CLI is required for tool-free inference')
    # Authentication stays in the CLI's subscription/keychain storage. Provider,
    # Todoist, Telegram and other ambient credentials are never inherited.
    env = {key: os.environ[key] for key in ('HOME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'TERM') if key in os.environ}
    env["PATH"] = os.pathsep.join([str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", env.get("PATH", "/usr/bin:/bin")])
    args = [binary, '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--disable-slash-commands', '--setting-sources', '',
            '--settings', json.dumps({'disableAllHooks': True}), '--no-session-persistence',
            '--system-prompt', system_prompt, '-p']
    with tempfile.TemporaryDirectory(prefix='bee-inference-') as directory:
        result = subprocess.run(args, input=user_message, capture_output=True, text=True,
                                timeout=timeout_s, env=env, cwd=directory)
    output = result.stdout.strip()
    if 'hit your session limit' in output.lower():
        raise RuntimeError('claude session limit hit; retry later')
    if result.returncode:
        # Do not propagate arbitrary CLI output containing private transcript data.
        raise RuntimeError(f'Claude inference failed (exit {result.returncode})')
    return output
