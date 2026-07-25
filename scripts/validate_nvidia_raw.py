#!/usr/bin/env python3
"""
Self-contained NVIDIA reachability check -- NO aios dependency.
Proves YOUR NVIDIA_API_KEY can reach deepseek-ai/deepseek-v4-pro on THIS
machine, using only the Python standard library (urllib).

This bypasses any repo version mismatch (the aios adapter lives in a newer
commit). It answers one question: "does my key + this machine's network
actually reach NVIDIA's inference endpoint?"

Usage (Windows CMD):
    set NVIDIA_API_KEY=nvapi-...
    .\.venv\Scripts\python scripts\validate_nvidia_raw.py

The script strips HTTPS_PROXY/HTTP_PROXY so a local blocking proxy does not
intercept the call, and validates the key is pure ASCII before sending
(non-ASCII in the key is the usual copy/paste mistake that breaks auth).
"""
import json
import os
import sys
import urllib.error
import urllib.request

# Drop proxy vars so urllib does not route through a blocking corporate proxy.
for _v in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
    os.environ.pop(_v, None)


def diagnose_key(key: str) -> int | None:
    """Return an exit code if the key is unusable, else None."""
    print(f"-> api_key len={len(key)}")
    print(f"-> api_key repr (first 24): {key[:24]!r}")
    bad = [(i, c, hex(ord(c))) for i, c in enumerate(key) if ord(c) > 127]
    if bad:
        print("\nNON-ASCII characters found in NVIDIA_API_KEY:")
        for i, c, h in bad[:12]:
            print(f"  pos={i} char={c!r} codepoint={h}")
        print("\nYour key contains non-ASCII characters -- almost always a")
        print("copy/paste artifact (invisible char, CJK, or full-width char).")
        print("Re-set it as PURE ASCII, e.g. (no quotes, no trailing spaces):")
        print("  set NVIDIA_API_KEY=nvapi-xxxxxxxxxxxx")
        return 3
    if not key.startswith("nvapi-"):
        print("\nWARNING: key does not start with 'nvapi-'. NVIDIA NIM keys")
        print("normally look like 'nvapi-...'. Double-check you copied the")
        print("right value (not a base_url or model name).")
    return None


def main() -> int:
    key = os.getenv("NVIDIA_API_KEY") or os.getenv("AIOS_AGENT_API_KEY")
    if not key:
        print("Set NVIDIA_API_KEY (or AIOS_AGENT_API_KEY) first.", file=sys.stderr)
        return 2

    code = diagnose_key(key)
    if code is not None:
        return code

    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    payload = {
        "model": "deepseek-ai/deepseek-v4-pro",
        "messages": [{"role": "user", "content": "Reply with exactly the word: OK"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")

    print(f"-> POST {url}")
    print("-> calling (proxy vars stripped) ...")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        reply = body["choices"][0]["message"]["content"].strip()
        print(f"\nHTTP 200 -- model replied: {reply!r}")
        print("SUCCESS: your NVIDIA key reaches deepseek-v4-pro on this machine.")
        return 0
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        print(f"\nHTTPError {exc.code}: {detail}")
        return 1
    except Exception as exc:  # timeout, connection reset, etc.
        print(f"\nERROR {type(exc).__name__}: {str(exc)[:400]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
