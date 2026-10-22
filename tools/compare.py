"""Word-for-word diff of a candidate transcript against a solaris-3 baseline.

Lines the two normalized word streams up in order (one global alignment,
immune to timestamp drift), flags every mismatch, marks glossary words as
CRITICAL, and writes a readable report: one block per differing baseline
utterance, with the audio time for ear-checking. No scores are computed.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# theologically important words and phrases; edit freely
CRITICAL_TERMS: list[str] = [
    "herod antipas", "pontius pilate", "john the baptist", "isaiah",
    "judea", "jerusalem", "jordan", "elijah", "ahab", "baal", "malachi",
    "babylonians", "exodus", "noah", "josephus", "perea", "galilee",
    "jericho", "matthew", "jesus", "emmanuel", "locusts", "camel hair",
    "apostles creed", "garry williams", "martin luther", "bavaria",
    "prophet", "baptist", "messiah", "verse one", "holy spirit",
]

# words inside multi-word terms that must not be flagged on their own
COMMON_WORDS: set[str] = {"the", "of", "in", "and", "a", "an", "is", "was", "to", "for"}


@dataclass(frozen=True)
class Match:
    base_index: int
    cand_index: int


@dataclass(frozen=True)
class Substitution:
    base_index: int
    cand_index: int


@dataclass(frozen=True)
class Deletion:
    base_index: int


@dataclass(frozen=True)
class Insertion:
    cand_index: int


Edit = Match | Substitution | Deletion | Insertion


@dataclass(frozen=True)
class Chunk:
    start: float
    end: float
    raw: str


@dataclass(frozen=True)
class CandidateWord:
    t: float
    text: str


def normalize_words(text: str) -> list[str]:
    lowered = text.lower().replace("\u2019", "'")
    stripped = re.sub(r"[^a-z0-9' -]+", " ", lowered)
    return [w.strip("' -") for w in stripped.split() if w.strip("' -")]


def is_critical_word(word: str) -> bool:
    if word in COMMON_WORDS:
        return False
    return any(word in term.split() for term in CRITICAL_TERMS)


def load_baseline(path: Path) -> list[Chunk]:
    data = json.loads(path.read_text())
    utterances = data["result"]["transcription"]["utterances"]
    return [
        Chunk(start=float(u["start"]), end=float(u["end"]), raw=u["text"].strip())
        for u in utterances
    ]


def load_candidate(path: Path) -> list[CandidateWord]:
    words: list[CandidateWord] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        words.append(CandidateWord(t=float(obj["t"]), text=str(obj["w"])))
    return words


def align(base: list[str], cand: list[str]) -> list[Edit]:
    """Minimal-edit pairing of two word sequences (Levenshtein backtrace)."""
    m, n = len(base), len(cand)
    dp: list[list[int]] = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            cost = 0 if base[i - 1] == cand[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j - 1] + cost,  # pair the words (match / substitution)
                dp[i - 1][j] + 1,  # baseline word unpaired -> missing
                dp[i][j - 1] + 1,  # candidate word unpaired -> extra
            )
    edits: list[Edit] = []
    i, j = m, n
    while i > 0 or j > 0:
        if (
            i > 0
            and j > 0
            and dp[i][j] == dp[i - 1][j - 1] + (0 if base[i - 1] == cand[j - 1] else 1)
        ):
            if base[i - 1] == cand[j - 1]:
                edits.append(Match(i - 1, j - 1))
            else:
                edits.append(Substitution(i - 1, j - 1))
            i -= 1
            j -= 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            edits.append(Deletion(i - 1))
            i -= 1
        else:
            edits.append(Insertion(j - 1))
            j -= 1
    edits.reverse()
    return edits


def fmt_ts(t: float) -> str:
    minutes, secs = divmod(t, 60.0)
    return f"{int(minutes):02d}:{secs:04.1f}"


def run_compare(
    baseline_path: Path,
    words_path: Path,
    model_name: str,
    out_path: Path,
    audio_label: str = "audio",
) -> tuple[int, int, int]:
    """Write the diff report; return (chunks_total, chunks_differing, critical_flags)."""
    chunks = load_baseline(baseline_path)
    candidate = load_candidate(words_path)

    # flatten the baseline into (word, chunk_index) pairs
    base_tokens: list[str] = []
    base_chunk: list[int] = []
    for ci, chunk in enumerate(chunks):
        for tok in normalize_words(chunk.raw):
            base_tokens.append(tok)
            base_chunk.append(ci)

    # candidate words, kept with display text
    cand_tokens: list[str] = []
    cand_display: list[str] = []
    for w in candidate:
        for tok in normalize_words(w.text):
            cand_tokens.append(tok)
            cand_display.append(w.text)

    edits = align(base_tokens, cand_tokens)

    subs = [0] * len(chunks)
    dels = [0] * len(chunks)
    ins = [0] * len(chunks)
    cand_by_chunk: list[list[str]] = [[] for _ in chunks]
    diffs_by_chunk: list[list[tuple[str, str, str]]] = [[] for _ in chunks]

    last_chunk = 0
    for edit in edits:
        if isinstance(edit, Deletion):
            ci = base_chunk[edit.base_index]
            dels[ci] += 1
            diffs_by_chunk[ci].append(
                ("del", base_tokens[edit.base_index], "")
            )
            last_chunk = ci
        elif isinstance(edit, Insertion):
            ci = last_chunk
            cand_by_chunk[ci].append(cand_display[edit.cand_index])
            ins[ci] += 1
            diffs_by_chunk[ci].append(
                ("ins", "", cand_tokens[edit.cand_index])
            )
        elif isinstance(edit, Substitution):
            ci = base_chunk[edit.base_index]
            cand_by_chunk[ci].append(cand_display[edit.cand_index])
            subs[ci] += 1
            diffs_by_chunk[ci].append(
                ("sub", base_tokens[edit.base_index], cand_tokens[edit.cand_index])
            )
            last_chunk = ci
        else:
            ci = base_chunk[edit.base_index]
            cand_by_chunk[ci].append(cand_display[edit.cand_index])
            last_chunk = ci

    report: list[str] = []
    differing = 0
    critical = 0
    for idx, chunk in enumerate(chunks):
        if subs[idx] + dels[idx] + ins[idx] == 0:
            continue
        differing += 1
        cand_raw = " ".join(cand_by_chunk[idx])
        base_norm = " ".join(
            tok for tok in normalize_words(chunk.raw)
        )
        cand_norm = " ".join(normalize_words(cand_raw))
        block: list[str] = [
            f"[chunk {idx + 1}] {fmt_ts(chunk.start)} - {fmt_ts(chunk.end)}",
            f"  base : {chunk.raw}",
            f"  cand : {cand_raw or '(nothing recognized)'}",
        ]
        for op, bw, cw in diffs_by_chunk[idx]:
            if op == "sub":
                mark = f"  diff : {bw} -> {cw}"
                if is_critical_word(bw):
                    mark += "   [CRITICAL]"
                block.append(mark)
            elif op == "del":
                mark = f"  diff : missing '{bw}'"
                if is_critical_word(bw):
                    mark += "   [CRITICAL]"
                block.append(mark)
            else:
                block.append(f"  diff : extra '{cw}'")
        for term in CRITICAL_TERMS:
            if term in base_norm and term not in cand_norm:
                flag = f"  CRITICAL TERM '{term}' in baseline, not found in candidate"
                if flag not in block:
                    block.append(flag)
                    critical += 1
        block.append(f"  audio: {audio_label} @{fmt_ts(chunk.start)}")
        report.append("\n".join(block))

    out: list[str] = [
        f"COMPARE: {model_name} vs solaris-3 baseline",
        f"baseline: {baseline_path}",
        f"chunks compared: {len(chunks)} | with differences: {differing}",
        f"critical term flags: {critical}",
        "",
        "=== CHUNKS WITH DIFFERENCES ===",
        "",
    ]
    out.extend(report)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out) + "\n")
    print(f"[{model_name}] {differing}/{len(chunks)} chunks differ, {critical} critical flags")
    return len(chunks), differing, critical


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="solaris-3 json")
    parser.add_argument("words", type=Path, help="candidate .words.jsonl")
    parser.add_argument("--name", required=True, help="label for the report")
    parser.add_argument("--out", type=Path, required=True, help="report path")
    parser.add_argument("--audio-label", default="audio")
    args = parser.parse_args()
    run_compare(args.baseline, args.words, args.name, args.out, args.audio_label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
