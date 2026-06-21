#!/usr/bin/env python3
"""
Remote experiment runner for OpenSearch LLM gate experiments.
Runs a diff through claude -p with full stream-json tracing, then POSTs results to callback URL.

Usage:
  python3 runner.py <diff_file> \
    --experiment-id a1-base64-encoding \
    --variant-id no_comment \
    --variant-label "No comment lines" \
    --callback-url https://xxxx.ngrok-free.app \
    [--config-json '{"has_comment": false}'] \
    [--notes "Optional notes for this run"] \
    [--run-index 1]
"""
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPT_FILE = os.path.join(SCRIPT_DIR, 'prompt.txt')


def parse_args():
    args = sys.argv[1:]
    diff_file = args[0] if args and not args[0].startswith('--') else None
    if not diff_file:
        print("ERROR: diff_file required as first argument", file=sys.stderr)
        sys.exit(1)

    def get(flag, default=None):
        try:
            i = args.index(flag)
            return args[i + 1]
        except (ValueError, IndexError):
            return default

    return {
        'diff_file': diff_file,
        'experiment_id': get('--experiment-id', 'unknown'),
        'variant_id': get('--variant-id', 'unknown'),
        'variant_label': get('--variant-label', get('--variant-id', 'unknown')),
        'callback_url': get('--callback-url'),
        'config_json': get('--config-json', '{}'),
        'notes': get('--notes', ''),
        'run_index': int(get('--run-index', '0')),
    }


def run_claude(prompt, diff_content):
    proc = subprocess.run(
        ['claude', '-p', prompt,
         '--model', 'sonnet',
         '--permission-mode=bypassPermissions',
         '--output-format=stream-json',
         '--verbose'],
        input=diff_content.encode(),
        capture_output=True,
        timeout=300,
    )
    return proc.stdout.decode(errors='replace'), proc.stderr.decode(errors='replace'), proc.returncode


def parse_stream_json(raw_stdout):
    tool_trace = []
    reasoning_parts = []
    num_turns = 0
    web_search = 0
    web_fetch = 0
    verdict_candidates = []

    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = obj.get('type')

        if t == 'assistant':
            for block in obj.get('message', {}).get('content', []):
                btype = block.get('type')
                if btype == 'tool_use':
                    tool_name = block.get('name', '')
                    tool_input = block.get('input', {})
                    tool_trace.append({
                        'type': 'tool_call',
                        'name': tool_name,
                        'input': tool_input,
                    })
                    print(f"  TOOL_CALL: {tool_name} | {json.dumps(tool_input)[:300]}")
                elif btype == 'thinking':
                    thinking = block.get('thinking', '')
                    if thinking.strip():
                        reasoning_parts.append(f"<thinking>\n{thinking}\n</thinking>")
                        print(f"  THINKING ({len(thinking)} chars): {thinking[:300]}")
                elif btype == 'text':
                    text = block.get('text', '')
                    if text.strip():
                        reasoning_parts.append(text)
                        # Check if this looks like a verdict
                        stripped = text.strip()
                        if stripped.startswith('{') and '"counts"' in stripped:
                            verdict_candidates.append(stripped)
                        print(f"  TEXT ({len(text)} chars): {text[:200]}")

        elif t == 'user':
            for block in obj.get('message', {}).get('content', []):
                if block.get('type') == 'tool_result':
                    content = block.get('content', '')
                    if isinstance(content, list):
                        content = '\n'.join(
                            c.get('text', '') for c in content if isinstance(c, dict)
                        )
                    tool_trace.append({
                        'type': 'tool_result',
                        'output': content,
                    })
                    print(f"  TOOL_RESULT: {str(content)[:300]}")

        elif t == 'result':
            usage = obj.get('usage', {})
            server = usage.get('server_tool_use', {})
            num_turns = obj.get('num_turns', 0)
            web_search = server.get('web_search_requests', 0)
            web_fetch = server.get('web_fetch_requests', 0)
            # Also check result output for verdict
            out = obj.get('result', '') or ''
            if out.strip().startswith('{') and '"counts"' in out:
                verdict_candidates.append(out.strip())

    return {
        'tool_trace': tool_trace,
        'reasoning_text': '\n---\n'.join(reasoning_parts),
        'num_turns': num_turns,
        'web_search': web_search,
        'web_fetch': web_fetch,
        'verdict_candidates': verdict_candidates,
    }


def extract_verdict(verdict_candidates, raw_stdout):
    # Try candidates in reverse order (last is most likely the final answer)
    for candidate in reversed(verdict_candidates):
        try:
            v = json.loads(candidate)
            if 'counts' in v and 'issues' in v:
                return v
        except json.JSONDecodeError:
            # Sometimes the output has trailing content — try to extract the JSON object
            try:
                start = candidate.index('{')
                # Find matching close brace
                depth = 0
                end = start
                for i, ch in enumerate(candidate[start:], start):
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                v = json.loads(candidate[start:end + 1])
                if 'counts' in v and 'issues' in v:
                    return v
            except (ValueError, json.JSONDecodeError):
                continue
    return None


def severity_level(verdict):
    if not verdict:
        return 0
    counts = verdict.get('counts', {})
    if counts.get('critical', 0) > 0:
        return 4
    if counts.get('high', 0) > 0:
        return 3
    if counts.get('medium', 0) > 0:
        return 2
    if counts.get('low', 0) > 0:
        return 1
    return 0


def post_result(callback_url, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{callback_url.rstrip('/')}/api/runs",
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            print(f"  DB: recorded run #{result.get('run_index')} for {payload['experiment_id']}/{payload['variant_id']}")
            return True
    except urllib.error.HTTPError as e:
        print(f"  DB ERROR {e.code}: {e.read().decode()}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  DB ERROR: {e}", file=sys.stderr)
        return False


def main():
    cfg = parse_args()

    print(f"\n{'='*60}")
    print(f"EXPERIMENT: {cfg['experiment_id']} / {cfg['variant_id']}")
    if cfg['run_index']:
        print(f"Run index: {cfg['run_index']}")
    print(f"{'='*60}")

    # Read inputs
    with open(PROMPT_FILE) as f:
        prompt = f.read().strip()

    with open(cfg['diff_file']) as f:
        diff_content = f.read()

    print(f"Diff: {len(diff_content)} chars, {diff_content.count(chr(10))} lines")
    print("Running claude -p with stream-json tracing...")

    # Run claude
    try:
        raw_stdout, raw_stderr, returncode = run_claude(prompt, diff_content)
    except subprocess.TimeoutExpired:
        print("ERROR: claude timed out after 300s", file=sys.stderr)
        raw_stdout, raw_stderr, returncode = '', 'TimeoutExpired', 1

    if raw_stderr.strip():
        print(f"STDERR: {raw_stderr[:500]}")

    # Parse stream-json
    parsed = parse_stream_json(raw_stdout)

    # Extract verdict
    verdict = extract_verdict(parsed['verdict_candidates'], raw_stdout)

    if verdict is None:
        print("WARNING: could not extract verdict JSON from output")
        print(f"Raw stdout ({len(raw_stdout)} chars):")
        print(raw_stdout[:2000])
        verdict = {"counts": {"total": 0, "critical": 0, "high": 0, "medium": 0, "low": 0},
                   "truncated": False, "issues": []}
        error_note = f"[PARSE ERROR: could not extract verdict] {cfg['notes']}"
    else:
        error_note = cfg['notes']

    counts = verdict.get('counts', {})
    sev = severity_level(verdict)
    gate = 'pass' if sev < 2 else 'fail'

    tools_used = [e['name'] for e in parsed['tool_trace'] if e['type'] == 'tool_call']
    print(f"\nRESULT: gate={gate} sev={sev} C={counts.get('critical',0)} H={counts.get('high',0)} "
          f"M={counts.get('medium',0)} L={counts.get('low',0)}")
    print(f"  turns={parsed['num_turns']} tools={tools_used or ['none']} "
          f"web_search={parsed['web_search']} web_fetch={parsed['web_fetch']}")

    if not cfg['callback_url']:
        print("\nNo --callback-url, skipping DB write.")
        print("Verdict:", json.dumps(verdict, indent=2)[:1000])
        return

    # Build config_json — merge with runner metadata
    try:
        config = json.loads(cfg['config_json'])
    except json.JSONDecodeError:
        config = {}
    config['num_turns'] = parsed['num_turns']
    config['tools_used'] = tools_used
    config['web_search'] = parsed['web_search']
    config['web_fetch'] = parsed['web_fetch']

    payload = {
        'experiment_id': cfg['experiment_id'],
        'variant_id': cfg['variant_id'],
        'variant_label': cfg['variant_label'],
        'diff_text': diff_content,
        'verdict_json': verdict,
        'counts_critical': counts.get('critical', 0),
        'counts_high': counts.get('high', 0),
        'counts_medium': counts.get('medium', 0),
        'counts_low': counts.get('low', 0),
        'severity_level': sev,
        'gate_result': gate,
        'config_json': config,
        'notes': error_note,
        'runner_type': 'runner',
        'tool_trace': parsed['tool_trace'],
        'reasoning_text': parsed['reasoning_text'],
    }

    print(f"\nPOSTing to {cfg['callback_url']}/api/runs ...")
    post_result(cfg['callback_url'], payload)


if __name__ == '__main__':
    main()
