"""Make one small Anthropic request, without logging credentials or retrying."""

import logging
import os
import re
import sys
from pathlib import Path

# Disable SDK/HTTP diagnostic logging before importing either dependency.
logging.disable(logging.CRITICAL)

import anthropic
from dotenv import load_dotenv


MODEL = "claude-haiku-4-5-20251001"
ENV_PATH = Path(__file__).resolve().parent / ".env"
EXPECTED_RESPONSE = "Walltown API connected."


def fail(message: str) -> int:
    """Print only messages written here, never raw exceptions or API bodies."""
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def placeholder_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in {
        "yourapikeyhere", "yourapikey", "youranthropicapikey",
        "youranthropicapikeyhere", "myanthropicapikey", "changeme",
        "placeholder", "example", "todo", "test", "dummy",
    } or bool(re.search(
        r"your[_ -]*(?:anthropic[_ -]*)?api[_ -]*key|replace[_ -]*(?:me|with)|<[^>]+>",
        key, re.IGNORECASE,
    ))


def api_failure(error: anthropic.APIStatusError) -> int:
    # Inspect error details only for classification; never print the details.
    body = error.body if isinstance(error.body, dict) else {}
    details = body.get("error", body)
    details = details if isinstance(details, dict) else {}
    description = " ".join(
        str(details.get(field, "")) for field in ("type", "message", "code")
    ).lower()
    if error.status_code == 402 or any(marker in description for marker in (
        "credit balance", "insufficient credit", "insufficient_credit",
        "insufficient_quota", "billing",
    )):
        return fail("Insufficient API credit or a billing restriction. No retry was made; billing settings were left unchanged.")
    if isinstance(error, anthropic.AuthenticationError):
        return fail("Authentication failed. Check ANTHROPIC_API_KEY in the root .env.")
    if isinstance(error, anthropic.PermissionDeniedError):
        return fail("Permission denied. Check the key's workspace permissions and model access.")
    if isinstance(error, anthropic.NotFoundError) or (
        "model" in description and any(marker in description for marker in (
            "not found", "not available", "not exist", "invalid", "access",
        ))
    ):
        return fail(f"Model access failed. Check access to {MODEL} for this workspace.")
    if isinstance(error, anthropic.RateLimitError):
        return fail("Rate limit reached. No automatic retry was made.")
    return fail(f"Anthropic rejected the request (HTTP {error.status_code}). No retry was made.")


def main() -> int:
    if not ENV_PATH.is_file():
        return fail("Root .env is missing. Add ANTHROPIC_API_KEY manually before running this test.")
    try:
        # A root .env without a key must not silently use an inherited key.
        os.environ.pop("ANTHROPIC_API_KEY", None)
        load_dotenv(ENV_PATH, override=True, interpolate=False)
    except Exception:
        return fail("Could not read the root .env. Check its permissions and syntax.")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key.strip():
        return fail("ANTHROPIC_API_KEY is missing or empty. Add it manually to the root .env.")
    if placeholder_key(key):
        return fail("ANTHROPIC_API_KEY is a placeholder. Replace it manually in the root .env.")
    if not key.startswith("sk-ant-") or any(char.isspace() for char in key):
        return fail("ANTHROPIC_API_KEY has an unexpected format or whitespace. Check the root .env.")

    print("API key: present (value hidden)", flush=True)
    print(f"Requested model: {MODEL}", flush=True)
    print("Making one Messages API request; automatic retries are disabled.", flush=True)
    try:
        with anthropic.Anthropic(
            api_key=key,
            base_url="https://api.anthropic.com",
            max_retries=0,
            timeout=20.0,
        ) as client:
            response = client.messages.create(
                model=MODEL,
                max_tokens=32,
                messages=[{
                    "role": "user",
                    "content": "Reply exactly: Walltown API connected.",
                }],
            )
    except anthropic.APITimeoutError:
        return fail("The API request timed out. Completion and usage are unknown; no retry was made.")
    except anthropic.APIConnectionError:
        return fail("Could not reach Anthropic. Check network, proxy, DNS, and TLS settings. No retry was made.")
    except anthropic.APIStatusError as error:
        return api_failure(error)
    except Exception:
        return fail("An unexpected client or response error occurred. No retry was made; usage may be unknown.")

    try:
        text = "".join(block.text for block in response.content if block.type == "text")
        print("Response:", text.replace(key, "[REDACTED]"))
        print("Model:", response.model)
        print("Input tokens:", response.usage.input_tokens)
        print("Output tokens:", response.usage.output_tokens)
        if response.model != MODEL or text != EXPECTED_RESPONSE:
            return fail("The request completed but the model or response did not match the expected result. No retry was made.")
    except Exception:
        return fail("The request completed but the response could not be validated. No retry was made.")
    print("PASS: Anthropic inference validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
