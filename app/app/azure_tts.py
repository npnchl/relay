"""Azure Speech TTS: Hindi text -> MP3 bytes, EU region (UK South).

Pure module - no Reflex imports. Uses the stdlib so there is nothing
extra to install. The voice is Aarav, a conversational Hindi neural
voice (verified available on the ebenezer-tts-test resource).
"""

import os
import urllib.request
from xml.sax.saxutils import escape

from . import env  # noqa: F401  (loads the repo .env)

VOICE = "hi-IN-AaravNeural"
OUTPUT_FORMAT = "audio-16khz-32kbitrate-mono-mp3"


def synthesize(text: str) -> bytes:
    """Speak one Hindi sentence; return MP3 bytes."""
    key = os.environ["AZURE_SPEECH_KEY"]
    region = os.environ["AZURE_SPEECH_REGION"]
    ssml = (
        '<speak version="1.0" xml:lang="hi-IN">'
        f'<voice xml:lang="hi-IN" name="{VOICE}">{escape(text)}</voice>'
        "</speak>"
    )
    req = urllib.request.Request(
        f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
        data=ssml.encode(),
        headers={
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": OUTPUT_FORMAT,
        },
    )
    with urllib.request.urlopen(req) as resp:
        return resp.read()
