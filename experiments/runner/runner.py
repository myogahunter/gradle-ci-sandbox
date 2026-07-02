#!/usr/bin/env python3
"""
Remote experiment runner for OpenSearch LLM gate experiments.
Matches OpenSearch's exact gate invocation: cat diff | claude -p "$PROMPT" --model sonnet
No --output-format=stream-json, no tool use, single-shot text output.

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
    # Exact match to OpenSearch's invocation:
    # cat "$DIFF_CONTENT_PATH" | claude -p "$PROMPT" > $DIFF_REPORT_PATH
    proc = subprocess.run(
        ['claude', '-p', prompt, '--model', 'sonnet'],
        input=diff_content.encode(),
        capture_output=True,
        timeout=300,
    )
    return proc.stdout.decode(errors='replace'), proc.stderr.decode(errors='replace'), proc.returncode


def extract_verdict(raw_stdout):
    # Output is plain text — find the JSON object in it
    text = raw_stdout.strip()
    if not text:
        return None
    # Find first { containing "counts"
    idx = text.find('{"counts"')
    if idx == -1:
        idx = text.find('{')
    if idx == -1:
        return None
    # Walk to find matching closing brace
    depth = 0
    end = idx
    for i, ch in enumerate(text[idx:], idx):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i
                break
    try:
        v = json.loads(text[idx:end + 1])
        if 'counts' in v and 'issues' in v:
            return v
    except json.JSONDecodeError:
        pass
    # Fallback: try the whole output as JSON
    try:
        v = json.loads(text)
        if 'counts' in v and 'issues' in v:
            return v
    except json.JSONDecodeError:
        pass
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

    with open(PROMPT_FILE) as f:
        prompt = f.read().strip()

    with open(cfg['diff_file']) as f:
        diff_content = f.read()

    print(f"Diff: {len(diff_content)} chars, {diff_content.count(chr(10))} lines")
    print("Running claude -p (single-shot, no tools, matches OpenSearch gate exactly)...")

    try:
        raw_stdout, raw_stderr, returncode = run_claude(prompt, diff_content)
    except subprocess.TimeoutExpired:
        print("ERROR: claude timed out after 300s", file=sys.stderr)
        raw_stdout, raw_stderr, returncode = '', 'TimeoutExpired', 1

    if raw_stderr.strip():
        print(f"STDERR: {raw_stderr[:500]}")

    print(f"Raw output ({len(raw_stdout)} chars): {raw_stdout[:300]}")

    verdict = extract_verdict(raw_stdout)

    if verdict is None:
        print("WARNING: could not extract verdict JSON from output")
        verdict = {"counts": {"total": 0, "critical": 0, "high": 0, "medium": 0, "low": 0},
                   "truncated": False, "issues": []}
        error_note = f"[PARSE ERROR: could not extract verdict] {cfg['notes']}"
    else:
        error_note = cfg['notes']

    counts = verdict.get('counts', {})
    sev = severity_level(verdict)
    gate = 'pass' if sev < 2 else 'fail'

    print(f"\nRESULT: gate={gate} sev={sev} C={counts.get('critical',0)} H={counts.get('high',0)} "
          f"M={counts.get('medium',0)} L={counts.get('low',0)}")

    if not cfg['callback_url']:
        print("\nNo --callback-url, skipping DB write.")
        print("Verdict:", json.dumps(verdict, indent=2)[:1000])
        return

    try:
        config = json.loads(cfg['config_json'])
    except json.JSONDecodeError:
        config = {}

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
        'tool_trace': [],
        'reasoning_text': raw_stdout,
    }

    print(f"\nPOSTing to {cfg['callback_url']}/api/runs ...")
    post_result(cfg['callback_url'], payload)


if __name__ == '__main__':
    main()
