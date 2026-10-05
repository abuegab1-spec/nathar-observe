#!/usr/bin/env python3
"""Read one bounded skill chunk; never writes files or claims agent comprehension."""
import argparse
import hashlib
import json
from pathlib import Path

from . import config

def chunk(path, offset=0, chars=3000, expected_hash=None):
    path = Path(path)
    path = (config.settings.workspace / path).resolve() if not path.is_absolute() else path.resolve()
    if not any(path.is_relative_to(root.resolve()) for root in config.settings.skills_roots) or path.name != "SKILL.md":
        raise ValueError("Expected a SKILL.md inside workspace skills")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_hash and expected_hash != digest:
        raise ValueError("Source changed; restart reading at offset zero")
    text = raw.decode("utf-8-sig")
    if not 0 <= offset <= len(text) or not 1 <= chars <= 4000:
        raise ValueError("Invalid offset or chunk size (1..4000)")
    end = min(offset + chars, len(text))
    while True:
        result = dict(path=str(path), sha256=digest, total_chars=len(text),
                      start_char=offset, end_char=end, next_offset=end,
                      eof=end == len(text), text=text[offset:end])
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded.encode("utf-8")) <= 7000:
            return result
        if end <= offset:
            raise ValueError("Metadata exceeds output budget")
        end = offset + (end - offset) // 2

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    config.add_arguments(parser)
    parser.add_argument("path")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--chars", type=int, default=3000)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    config.apply_arguments(args)
    try:
        result = chunk(args.path, args.offset, args.chars, args.sha256)
    except (OSError, ValueError, UnicodeError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()
