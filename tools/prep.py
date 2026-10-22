"""Convert a recording to 16 kHz mono 16-bit WAV (what the model needs)."""

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="input audio file")
    parser.add_argument("output", type=Path, help="output 16 kHz mono WAV")
    args = parser.parse_args()
    subprocess.run(
        [
            "afconvert",
            "-f", "WAVE",
            "-d", "LEI16@16000",
            "-c", "1",
            str(args.source),
            str(args.output),
        ],
        check=True,
    )
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
