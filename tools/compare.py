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


# contractions expanded before possessive stripping, so "it's" never
# loses its meaning ("it is") while "Jerusalem's" becomes "jerusalem"
CONTRACTIONS: dict[str, list[str]] = {
    "where's": ["where", "is"], "what's": ["what", "is"],
    "who's": ["who", "is"], "he's": ["he", "is"], "she's": ["she", "is"],
    "it's": ["it", "is"], "that's": ["that", "is"], "there's": ["there", "is"],
    "how's": ["how", "is"], "what're": ["what", "are"],
    "gonna": ["going", "to"], "wanna": ["want", "to"],
}

NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000, "million": 1_000_000,
}
# spoken zero inside a number ("one oh one" == "101")
SPOKEN_ZERO = {"oh", "o", "zero"}


def expand_tokens(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for tok in tokens:
        out.extend(CONTRACTIONS.get(tok, [tok]))
    return out


def strip_possessives(tokens: list[str]) -> list[str]:
    # "jerusalem's" -> "jerusalem"; trailing-apostrophe forms handled earlier
    return [tok[:-2] if tok.endswith("'s") and len(tok) > 3 else tok for tok in tokens]


def is_number_word(tok: str) -> bool:
    return tok.isdigit() or tok in NUMBER_WORDS or tok in SPOKEN_ZERO


def parse_number(run: list[str]) -> int | None:
    """Value of a run of number words/digits, or None if not parseable."""
    if not run:
        return None
    # digit strings like "2000" pass straight through
    if len(run) == 1 and run[0].isdigit():
        return int(run[0])
    # digit-spelled style: "one oh one" -> 101 (all items single digits)
    single: dict[str, str] = {
        **{k: str(v) for k, v in NUMBER_WORDS.items() if v < 10},
        "oh": "0", "o": "0", "zero": "0",
    }
    if all(w in single for w in run) and len(run) >= 2:
        return int("".join(single[w] for w in run))
    # value style: "two thousand" -> 2000, "twenty five" -> 25
    total = 0
    current = 0
    for w in run:
        if w.isdigit():
            current = current * 10 + int(w) if current else int(w)
        elif w in SPOKEN_ZERO:
            current = current * 10 if current else 0
        elif w in NUMBER_WORDS:
            v = NUMBER_WORDS[w]
            if v == 100:
                current = max(current, 1) * 100
            elif v >= 1000:
                total += max(current, 1) * v
                current = 0
            else:
                current += v
        else:
            return None
    return total + current


def collapse_numbers(tokens: list[str]) -> list[str]:
    """Consecutive number words/digits become one canonical token: #<value>."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            value = parse_number(run)
            if value is not None:
                out.append(f"#{value}")
            else:
                out.extend(run)
            run.clear()

    for i, tok in enumerate(tokens):
        if tok.isdigit() or tok in NUMBER_WORDS:
            run.append(tok)
        elif tok in SPOKEN_ZERO and (run or (i + 1 < len(tokens) and is_number_word(tokens[i + 1]))):
            run.append(tok)
        else:
            flush()
            out.append(tok)
    flush()
    return out


def normalize_words(text: str) -> list[str]:
    lowered = text.lower().replace("\u2019", "'")
    # hyphens split too: "once-off" and "once off" are the same spoken phrase
    lowered = lowered.replace("-", " ")
    stripped = re.sub(r"[^a-z0-9' ]+", " ", lowered)
    tokens = [w.strip("' ") for w in stripped.split() if w.strip("' ")]
    tokens = expand_tokens(tokens)
    tokens = strip_possessives(tokens)
    return collapse_numbers(tokens)


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

    # compound joins: "chat gpt" == "chatgpt" - if one side writes two
    # adjacent tokens that the other side writes as one word, merge them
    cand_set = set(cand_tokens)
    merged_base: list[str] = []
    merged_base_chunk: list[int] = []
    i = 0
    while i < len(base_tokens):
        if (
            i + 1 < len(base_tokens)
            and base_tokens[i] + base_tokens[i + 1] in cand_set
        ):
            merged_base.append(base_tokens[i] + base_tokens[i + 1])
            merged_base_chunk.append(base_chunk[i])
            i += 2
        else:
            merged_base.append(base_tokens[i])
            merged_base_chunk.append(base_chunk[i])
            i += 1
    base_tokens = merged_base
    base_chunk = merged_base_chunk
    base_set = set(base_tokens)
    merged_cand: list[str] = []
    merged_display: list[str] = []
    j = 0
    while j < len(cand_tokens):
        if (
            j + 1 < len(cand_tokens)
            and cand_tokens[j] + cand_tokens[j + 1] in base_set
        ):
            merged_cand.append(cand_tokens[j] + cand_tokens[j + 1])
            merged_display.append(cand_display[j] + cand_display[j + 1])
            j += 2
        else:
            merged_cand.append(cand_tokens[j])
            merged_display.append(cand_display[j])
            j += 1
    cand_tokens = merged_cand
    cand_display = merged_display

    edits = align(base_tokens, cand_tokens)

    subs = [0] * len(chunks)
    dels = [0] * len(chunks)
    ins = [0] * len(chunks)
    cand_by_chunk: list[list[str]] = [[] for _ in chunks]
    diffs_by_chunk: list[list[tuple[str, str, str]]] = [[] for _ in chunks]

    last_chunk = 0
    base_pos = 0  # base words consumed so far; orphans sit at this position
    for edit in edits:
        if isinstance(edit, Deletion):
            ci = base_chunk[edit.base_index]
            dels[ci] += 1
            diffs_by_chunk[ci].append(
                ("del", base_tokens[edit.base_index], "")
            )
            last_chunk = ci
            base_pos += 1
        elif isinstance(edit, Insertion):
            prev_chunk = base_chunk[base_pos - 1] if base_pos > 0 else None
            next_chunk = base_chunk[base_pos] if base_pos < len(base_chunk) else None
            if next_chunk is not None and next_chunk != prev_chunk:
                # an orphan at a sentence boundary belongs with what follows
                ci = next_chunk
            else:
                ci = last_chunk
            cand_by_chunk[ci].append(cand_display[edit.cand_index])
            ins[ci] += 1
            diffs_by_chunk[ci].append(
                ("ins", "", cand_tokens[edit.cand_index])
            )
            if next_chunk is not None:
                last_chunk = ci
        elif isinstance(edit, Substitution):
            ci = base_chunk[edit.base_index]
            cand_by_chunk[ci].append(cand_display[edit.cand_index])
            subs[ci] += 1
            diffs_by_chunk[ci].append(
                ("sub", base_tokens[edit.base_index], cand_tokens[edit.cand_index])
            )
            last_chunk = ci
            base_pos += 1
        else:
            ci = base_chunk[edit.base_index]
            cand_by_chunk[ci].append(cand_display[edit.cand_index])
            last_chunk = ci
            base_pos += 1

    report: list[str] = []
    differing = 0
    critical = 0
    for idx, chunk in enumerate(chunks):
        if subs[idx] + dels[idx] + ins[idx] == 0:
            continue
        differing += 1
        cand_raw = " ".join(cand_by_chunk[idx])
        # display both sides in the same primitive form that was compared,
        # so visible differences are always real differences
        base_display = " ".join(normalize_words(chunk.raw))
        cand_display_text = " ".join(normalize_words(cand_raw))
        base_norm = base_display
        cand_norm = cand_display_text
        block: list[str] = [
            f"[chunk {idx + 1}] {fmt_ts(chunk.start)} - {fmt_ts(chunk.end)}",
            f"  base : {base_display}",
            f"  cand : {cand_display_text or '(nothing recognized)'}",
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
