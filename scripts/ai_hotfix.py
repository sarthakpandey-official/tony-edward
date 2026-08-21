#!/usr/bin/env python3
"""
scripts/ai_hotfix.py — AI Self-Healing Hotfix Generator.

Receives a Sentry webhook payload (or test failure context), uses the
Nvidia Nemotron (free tier) LLM (via Requesty router) to generate a
minimal code patch, and applies the patch to a hotfix branch.

LLM Failover:
  1. Try PRIMARY_LLM_API_KEY first.
  2. On 401/402/403/429, automatically retry with PRIMARY_LLM_API_KEY_FALLBACK.
  3. If both fail, exit with non-zero status (workflow will retry).

Usage:
  python scripts/ai_hotfix.py --issue-id <id> --error-file <path> [--repo-root .]

Inputs (via --error-file):
  A JSON file containing the failure context:
  {
    "issue_id": "abc123",
    "stack_trace": "Traceback (most recent call last):\n  File ...",
    "error_message": "KeyError: 'foo'",
    "file_path": "app/api/v1/search.py",
    "line_number": 42,
    "code_context": "...lines around the error..."
  }

Outputs:
  - A unified diff patch file at /tmp/hotfix-<issue_id>.patch
  - Applies the patch to the working tree (git apply)
  - Prints JSON to stdout: {"ok": true, "patch_path": "...", "files_changed": [...]}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

# LLM configuration — Requesty router → Nvidia Nemotron (FREE tier)
# NOTE: nemotron-3-ultra-550b-a55b is PAID. We use the free tier:
# - nvidia/nemotron-3-super-120b-a12b (primary: 120B params, good for code patches)
# - nvidia/nemotron-3-nano-30b-a3b (fallback: smaller, faster)
LLM_API_URL = os.environ.get("PRIMARY_LLM_API_URL", "https://router.requesty.ai/v1")
LLM_MODEL = os.environ.get("PRIMARY_LLM_MODEL", "nvidia/nemotron-3-super-120b-a12b")
LLM_FALLBACK_MODEL = os.environ.get("PRIMARY_LLM_FALLBACK_MODEL", "nvidia/nemotron-3-nano-30b-a3b")
# Free Nemotron models respond in 5-60s. Bump timeout to 6 min for safety.
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "360"))

PRIMARY_KEY = os.environ.get("PRIMARY_LLM_API_KEY", "")
FALLBACK_KEY = os.environ.get("PRIMARY_LLM_API_KEY_FALLBACK", "")

# Status codes that trigger automatic failover
_FAILOVER_STATUSES = {401, 402, 403, 429}


def _call_llm(messages: list[dict], api_key: str, model: str = None) -> tuple[Optional[str], int, Optional[str]]:
    """Call LLM with one key + one model. Returns (text, status_code, error).

    Retries up to 3 times on transient network errors (RemoteProtocolError,
    ReadTimeout, ConnectError) before giving up.
    """
    import time as _time
    model = model or LLM_MODEL
    transient_errors = ("RemoteProtocolError", "ReadTimeout", "ConnectError",
                        "ReadError", "ConnectionClosed", "PoolTimeout")
    last_err: Optional[str] = None
    last_status: int = 0
    for attempt in range(3):
        try:
            with httpx.Client(timeout=httpx.Timeout(LLM_TIMEOUT, connect=30.0, read=LLM_TIMEOUT, write=60.0)) as client:
                resp = client.post(
                    f"{LLM_API_URL.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": 0.0,
                        "max_tokens": 4000,
                    },
                )
            if resp.status_code >= 400:
                return None, resp.status_code, f"HTTP {resp.status_code}: {resp.text[:200]}"
            data = resp.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return text, resp.status_code, None
        except httpx.HTTPError as exc:
            err_name = type(exc).__name__
            last_err = f"{err_name}: {exc}"
            last_status = 0
            # Retry on transient errors
            if any(te in err_name for te in transient_errors) and attempt < 2:
                wait_sec = 5 * (attempt + 1)
                print(f"[hotfix] {err_name} on attempt {attempt+1}/3 — waiting {wait_sec}s before retry", file=sys.stderr)
                _time.sleep(wait_sec)
                continue
            return None, 0, f"{err_name}: {exc}"
    return None, last_status, last_err or "all_retries_failed"


def call_llm_with_failover(messages: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """Try primary key first, fall back to fallback key on 401/402/403/429.

    Also switches to a fallback (smaller, free) model when using the fallback key.

    Returns (text, error). On success, error is None.
    """
    keys_to_try = [k for k in [PRIMARY_KEY, FALLBACK_KEY] if k]
    if not keys_to_try:
        return None, "no_llm_api_key_configured"

    # Pair each key with a model: primary key → primary model, fallback key → fallback (smaller) model
    key_model_pairs = []
    if PRIMARY_KEY:
        key_model_pairs.append((PRIMARY_KEY, LLM_MODEL, "primary"))
    if FALLBACK_KEY:
        # Fallback key uses fallback model too (smaller = more reliable on free tier)
        key_model_pairs.append((FALLBACK_KEY, LLM_FALLBACK_MODEL, "fallback"))

    last_err: Optional[str] = None
    for i, (key, model, label) in enumerate(key_model_pairs):
        # Use the model-specific messages — override the model in messages doesn't work,
        # we pass it as a separate parameter via _call_llm
        print(f"[hotfix] LLM attempt {i+1}/{len(key_model_pairs)} | key={label} model={model} key_suffix=...{key[-6:]}", file=sys.stderr)
        text, status, err = _call_llm(messages, key, model)
        if text is not None:
            return text, None
        last_err = err
        # If failover status and we have more keys to try, retry
        if status in _FAILOVER_STATUSES and i < len(key_model_pairs) - 1:
            print(f"[hotfix] status={status} — failing over to next key + smaller model", file=sys.stderr)
            continue
        # Non-failover error or last key — return the error
        return None, err or f"unknown_error_status_{status}"
    return None, last_err or "all_keys_exhausted"


def build_hotfix_prompt(failure: dict) -> list[dict]:
    """Build the structured prompt for the LLM to generate a minimal patch.

    The prompt:
      1. Provides full diagnostic context (stack trace, error, file, line).
      2. Includes the actual code around the failing line.
      3. Asks for a MINIMAL diff that fixes the bug without unrelated changes.
      4. Asks the LLM to output the patch in unified diff format.
    """
    stack = failure.get("stack_trace", "N/A")
    error_msg = failure.get("error_message", failure.get("exception", {}).get("value", "N/A"))
    file_path = failure.get("file_path", "unknown")
    line_number = failure.get("line_number", "unknown")
    code_context = failure.get("code_context", "")

    # If code_context is empty but we have a repo root, read the file
    if not code_context and file_path and file_path != "unknown":
        try:
            repo_root = failure.get("repo_root", ".")
            full_path = Path(repo_root) / file_path
            if full_path.exists():
                lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
                start = max(0, int(line_number) - 11) if isinstance(line_number, int) else 0
                end = min(len(lines), int(line_number) + 10) if isinstance(line_number, int) else len(lines)
                code_context = "\n".join(
                    f"{i+1:4d} | {lines[i]}" for i in range(start, end)
                )
        except Exception as exc:
            code_context = f"(could not read file: {exc})"

    system_prompt = (
        "You are a Principal Python Engineer specializing in FastAPI codebases. "
        "Your job is to generate MINIMAL, SURGICAL code patches that fix bugs without "
        "modifying unrelated functionality.\n\n"
        "Rules:\n"
        "1. Output ONLY a unified diff patch (the kind `git diff` produces).\n"
        "2. Do NOT add comments unless absolutely necessary.\n"
        "3. Do NOT refactor or rename unrelated variables.\n"
        "4. Do NOT add new dependencies.\n"
        "5. The patch must be immediately applicable via `git apply`.\n"
        "6. If you cannot determine the fix with certainty, output the single line: `NO_FIX_AVAILABLE`.\n"
        "7. Wrap your diff in ```diff ... ``` code fences.\n"
        "8. Match the file's existing indentation style exactly.\n"
        "9. The diff header must use real paths relative to repo root.\n"
        "10. Keep the patch as small as possible — ideally under 20 lines."
    )

    user_prompt = f"""Fix this bug in the Tony-EDWARD FastAPI codebase.

## Error Details

**Error:** {error_msg}
**File:** `{file_path}`
**Line:** {line_number}

## Stack Trace

```
{stack}
```

## Code Context

```
{code_context}
```

## Instructions

Generate a minimal unified diff patch that fixes this specific bug.
Do NOT change anything unrelated. Output the patch in a ```diff code fence.
"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def extract_patch_from_response(text: str) -> Optional[str]:
    """Extract the unified diff from the LLM response.

    Handles:
      - Code fences ```diff ... ```
      - Raw diff starting with '---'/'diff --git'
      - 'NO_FIX_AVAILABLE' sentinel
    """
    if not text:
        return None
    if "NO_FIX_AVAILABLE" in text:
        return None

    # Try to extract from ```diff ... ``` fence
    fence_match = re.search(r"```(?:diff)?\s*\n(.*?)```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip() + "\n"

    # Try to find raw diff content (starts with 'diff --git' or '--- ')
    lines = text.strip().split("\n")
    diff_lines = []
    in_diff = False
    for line in lines:
        if line.startswith("diff --git") or (line.startswith("--- ") and not in_diff):
            in_diff = True
        if in_diff:
            diff_lines.append(line)
    if diff_lines:
        return "\n".join(diff_lines) + "\n"

    return None


def save_patch(patch_text: str, issue_id: str) -> Path:
    """Save the patch to a temp file and return its path."""
    patch_path = Path(f"/tmp/hotfix-{issue_id}.patch")
    patch_path.write_text(patch_text, encoding="utf-8")
    return patch_path


def apply_patch(patch_path: Path, repo_root: str) -> tuple[bool, str]:
    """Apply the patch via `git apply`. Returns (success, output)."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "apply", "--check", str(patch_path)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False, f"Patch check failed: {result.stderr}"
        # Apply for real
        result = subprocess.run(
            ["git", "apply", str(patch_path)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False, f"Patch apply failed: {result.stderr}"
        return True, "Patch applied successfully"
    except subprocess.SubprocessError as exc:
        return False, f"subprocess error: {exc}"


def list_changed_files(repo_root: str) -> list[str]:
    """List files modified by the applied patch."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return [f for f in result.stdout.strip().split("\n") if f]
    except Exception:
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Hotfix Generator")
    parser.add_argument("--issue-id", required=True, help="Unique issue ID (used in branch name)")
    parser.add_argument("--error-file", required=True, help="JSON file with failure context")
    parser.add_argument("--repo-root", default=".", help="Repository root directory")
    args = parser.parse_args()

    # Load failure context
    error_path = Path(args.error_file)
    if not error_path.exists():
        print(json.dumps({"ok": False, "error": f"error_file_not_found: {args.error_file}"}))
        return 2

    failure = json.loads(error_path.read_text(encoding="utf-8"))
    failure["repo_root"] = args.repo_root

    print(f"[hotfix] Generating patch for issue {args.issue_id}", file=sys.stderr)

    # Build prompt
    messages = build_hotfix_prompt(failure)

    # Call LLM with failover
    response_text, error = call_llm_with_failover(messages)
    if response_text is None:
        print(json.dumps({"ok": False, "error": error}))
        return 3

    print(f"[hotfix] LLM response received ({len(response_text)} chars)", file=sys.stderr)

    # Extract patch
    patch_text = extract_patch_from_response(response_text)
    if patch_text is None:
        print(json.dumps({"ok": False, "error": "no_fix_available_or_could_not_parse"}))
        return 4

    # Save patch
    patch_path = save_patch(patch_text, args.issue_id)
    print(f"[hotfix] Patch saved to {patch_path}", file=sys.stderr)

    # Apply patch
    ok, msg = apply_patch(patch_path, args.repo_root)
    if not ok:
        print(json.dumps({"ok": False, "error": msg, "patch_path": str(patch_path)}))
        return 5

    # List changed files
    changed_files = list_changed_files(args.repo_root)

    print(json.dumps({
        "ok": True,
        "patch_path": str(patch_path),
        "files_changed": changed_files,
        "patch_size_bytes": patch_path.stat().st_size,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
