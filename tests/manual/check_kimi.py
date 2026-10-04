#!/usr/bin/env python3
"""Manually verify the Kimi API key, endpoint, model, and basic completion."""

import os
from pathlib import Path
import sys
import time

from dotenv import load_dotenv
from openai import OpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_MODEL = "kimi-k3"


def main() -> int:
    load_dotenv(ENV_FILE)
    api_key = os.environ.get("MOONSHOT_API_KEY")
    model = os.environ.get("LLM_MODEL") or os.environ.get("KIMI_MODEL", DEFAULT_MODEL)

    if not api_key:
        print("FAIL: MOONSHOT_API_KEY is missing from .env or the environment")
        return 1

    client = OpenAI(
        api_key=api_key,
        base_url=BASE_URL,
        timeout=120.0,
        max_retries=0,
    )

    print(f"endpoint: {BASE_URL}")
    print(f"model: {model}")

    try:
        started = time.monotonic()
        available_models = {item.id for item in client.models.list().data}
        elapsed = time.monotonic() - started
        print(f"authentication: OK ({elapsed:.1f}s)")

        if model not in available_models:
            print(f"FAIL: model {model!r} is not available for this API key")
            return 1

        started = time.monotonic()
        options = (
            {"reasoning_effort": os.environ.get("LLM_REASONING_EFFORT", "low")}
            if model.startswith("kimi-k3")
            else {"extra_body": {"thinking": {"type": "disabled"}}}
        )
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": "Reply with exactly: KIMI_OK",
                }
            ],
            max_completion_tokens=1024,
            **options,
        )
        elapsed = time.monotonic() - started
        content = response.choices[0].message.content or ""
        print(f"completion: {content.strip()} ({elapsed:.1f}s)")

        if "KIMI_OK" not in content:
            print("FAIL: Kimi responded, but the response was unexpected")
            return 1
    except Exception as error:
        print(f"FAIL: {type(error).__name__}: {error}")
        return 1

    print("PASS: Kimi connection is working")
    return 0


if __name__ == "__main__":
    sys.exit(main())
