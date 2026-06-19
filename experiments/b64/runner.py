#!/usr/bin/env python3
"""
Runs a claude -p analysis and prints structured trace of tool calls and response.
Usage: python3 runner.py <diff_file> [--decode-hint]
"""
import subprocess, json, sys, os

diff_file = sys.argv[1]
decode_hint = '--decode-hint' in sys.argv

with open('experiments/b64/prompt.txt') as f:
    prompt = f.read().strip()

if decode_hint:
    prompt += "\n\nFor any base64 strings found: decode them using the Bash tool and include the decoded value in your analysis."

with open(diff_file) as f:
    diff_content = f.read()

proc = subprocess.run(
    ['claude', '-p', prompt, '--permission-mode=bypassPermissions',
     '--output-format=stream-json', '--verbose'],
    input=diff_content.encode(),
    capture_output=True
)

tools_called = []
for line in proc.stdout.decode().splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except:
        continue
    t = obj.get('type')
    if t == 'assistant':
        for b in obj.get('message', {}).get('content', []):
            if b.get('type') == 'tool_use':
                tools_called.append(b['name'])
                inp = json.dumps(b.get('input', {}))[:200]
                print(f"TOOL_CALLED: {b['name']} | {inp}")
            elif b.get('type') == 'text':
                print(f"TEXT: {b['text'][:600]}")
    elif t == 'user':
        for b in obj.get('message', {}).get('content', []):
            if b.get('type') == 'tool_result':
                rc = b.get('content', '')
                if isinstance(rc, list):
                    rc = ' '.join(r.get('text', '') for r in rc)
                print(f"TOOL_RESULT: {str(rc)[:200]}")
    elif t == 'result':
        u = obj.get('usage', {})
        s = u.get('server_tool_use', {})
        print(f"SUMMARY: turns={obj['num_turns']} tools={tools_called or ['none']} "
              f"web_search={s.get('web_search_requests',0)} web_fetch={s.get('web_fetch_requests',0)}")
