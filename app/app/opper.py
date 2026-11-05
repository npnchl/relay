"""Claude Haiku 5.5 translation via the Opper AI gateway (EU route).

One OpenAI-compatible HTTPS call per sentence to
https://api.opper.ai/v3/compat/chat/completions using the AWS Bedrock
EU route's model id (aws/claude-haiku-5-5-eu), so inference stays in
the EU. Pure provider logic - no Reflex imports, stdlib only, matching
azure_tts.py. The system prompt is the deliberately minimal v1
baseline; a glossary-enriched v2 is the planned A/B experiment.
"""

import json
import os
import re
import urllib.request

from . import env  # noqa: F401  (loads the repo .env)

ENDPOINT = "https://api.opper.ai/v3/compat/chat/completions"
MODEL_ID = "aws/claude-haiku-5-5-eu"  # AWS Bedrock EU route on Opper
SYSTEM_PROMPT = (
    "You will receive a JSON object with two fields: \"context\" "
    "(the sentences said just before, in order) and \"to_translate\" "
    "(the current sentence). Translate only the to_translate sentence "
    "from English to Hindi, using the context for terminology and "
    "consistency.\n"
    "The text is a live church sermon: translate in natural, spoken "
    "Hindi that a congregation can read as live captions.\n"
    'Respond with only a JSON object and nothing else: '
    '{"translated_sentence": "<the Hindi translation>"}'
)
TIMEOUT_S = 30


def translate(text: str, context: list | None = None) -> str:
    """One whole English sentence in, one Hindi sentence out.

    `context` is a list of (english, hindi) pairs from previous
    sentences; the request sends them as a JSON payload:
    {"context": [...], "to_translate": text}.

    Raises on provider errors (auth, routing, throttling) - the caller
    decides how to surface them.
    """
    ctx = [
        (item[0] if isinstance(item, (list, tuple)) else item)
        for item in (context or [])
    ][-5:]
    payload = {"context": ctx, "to_translate": text}
    body = {
        "model": MODEL_ID,
        "max_tokens": 2048,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['OPPER_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        out = json.loads(resp.read())
    return parse_translation(out["choices"][0]["message"]["content"])


def parse_translation(raw: str) -> str:
    """Extract the translation from the model's reply.

    The prompt asks for a bare JSON object, but models sometimes wrap it
    in prose or code fences - so scan for the outermost {...} block and
    parse that. If nothing parses, fall back to the raw text so a live
    stream never breaks; the untranslated-English badge in the UI still
    flags any English that leaks through.
    """
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            value = obj.get("translated_sentence")
            if isinstance(value, str) and value.strip():
                return value.strip()
        except (json.JSONDecodeError, AttributeError):
            pass
    # truncated JSON (output cut mid-string, no closing brace/quote):
    # pull the translated_sentence value out by regex instead
    m = re.search(
        r'"translated_sentence"\s*:\s*"((?:[^"\\]|\\.)*)', text, re.DOTALL
    )
    if m:
        try:
            return json.loads('"' + m.group(1) + '"').strip()
        except json.JSONDecodeError:
            return m.group(1).strip()
    return text
