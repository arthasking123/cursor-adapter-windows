from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise RuntimeError(f"HTTP {e.code}: {raw}") from e


def main() -> int:
    p = argparse.ArgumentParser(description="Test OpenAI-compatible chat endpoint.")
    p.add_argument("--base-url", default="http://127.0.0.1:17325", help="Base URL of adapter")
    p.add_argument("--model", default="cursor-window", help="Model id")
    p.add_argument("--system", default="You are a helpful assistant.", help="System prompt")
    p.add_argument("--user", default="Say HELLOWORLD", help="User message")
    args = p.parse_args()

    url = args.base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": args.system},
            {"role": "user", "content": args.user},
        ],
        "stream": False,
    }

    data = _post_json(url, payload)
    print("=== assistant content ===")
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        content = None
    print(content if content is not None else "(no content)")
    print()
    print("=== raw response ===")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

