"""The reference plugin.

Deliberately dull. Its job is to show the protocol and to be something the
tests can prove the host's limits against, not to be useful.

A plugin is a plain Python process that reads one JSON object per line from
stdin and writes one per line to stdout:

    in:  {"tool": "word_count", "args": {"text": "hello world"}}
    out: {"ok": true, "data": {"summary": "2 words", "words": 2}}

It is started with `python -s -m <entry>` from inside its own folder, with
almost no environment: no API keys, no tokens, nothing inherited from Jarvis.
Printing anything other than the response line to stdout would corrupt the
protocol, so diagnostics go to stderr, where the host reports them if the
plugin fails.

This plugin declares no scopes at all, which is what a plugin that only
transforms the text it was handed should do.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from typing import Any

WORD = re.compile(r"[\w'-]+", re.UNICODE)
MAX_TEXT = 200_000


def word_count(args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("text", ""))
    if not text.strip():
        raise ValueError("No text was given to count.")
    if len(text) > MAX_TEXT:
        raise ValueError(f"That text is {len(text)} characters; this plugin handles {MAX_TEXT}.")

    words = WORD.findall(text)
    top = max(1, min(int(args.get("top", 5)), 20))
    common = Counter(w.lower() for w in words if len(w) > 2).most_common(top)
    lines = text.count("\n") + 1

    return {
        "summary": (
            f"{len(words)} words, {lines} lines, {len(text)} characters"
            + (f"; most common: {', '.join(w for w, _ in common[:3])}" if common else "")
        ),
        "words": len(words),
        "lines": lines,
        "characters": len(text),
        "unique": len({w.lower() for w in words}),
        "mostCommon": [{"word": w, "count": n} for w, n in common],
    }


TOOLS = {"word_count": word_count}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            handler = TOOLS.get(str(request.get("tool", "")))
            if handler is None:
                response = {"ok": False, "error": f"Unknown tool {request.get('tool')!r}."}
            else:
                response = {"ok": True, "data": handler(request.get("args") or {})}
        except Exception as exc:  # one bad request must not end the process
            response = {"ok": False, "error": str(exc)}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
