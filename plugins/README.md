# Plugins

A plugin is a folder containing `plugin.json` and the Python module it names.
Jarvis discovers folders here, lists them, and runs **none** of them until you
enable one.

## What a plugin can and cannot do

A plugin runs in its own process with almost no environment: no API keys, no
tokens, nothing inherited from Jarvis. It cannot reach Jarvis's objects, its
policy engine or its credential store — it can only send messages.

Its tools go through exactly the same gate as the built-in ones, with a ceiling
on top: a plugin may never declare a critical-risk tool, and its effective
permissions are the intersection of what its manifest declares, what you
approved when you enabled it, and what Jarvis itself holds. Revoke a permission
from Jarvis and every plugin loses it too.

**This is a process boundary, not a security sandbox.** A plugin runs as you,
with your file access and your network. A malicious plugin can read your
documents directly without asking Jarvis. The scope system governs what it can
do *through Jarvis*, which is a real boundary and not a substitute for trusting
whoever wrote the plugin. Proper confinement needs an OS sandbox and is not
built.

## The manifest

```json
{
  "name": "word-count",
  "version": "1.0.0",
  "description": "What it does, in a sentence.",
  "author": "You",
  "entry": "main",
  "tools": [
    {
      "name": "word_count",
      "description": "What this tool does.",
      "risk": "safe",
      "scopes": [],
      "inputSchema": { "type": "object" }
    }
  ]
}
```

`risk` is one of `safe`, `low`, `medium`, `high`. `critical` is refused.
`scopes` are Jarvis capability names (`fs.read`, `browser.use`, …); an
unrecognised one is refused rather than ignored, because a permission the host
does not understand is one it cannot enforce.

## The protocol

One JSON object per line in on stdin, one out on stdout:

```
in:  {"tool": "word_count", "args": {"text": "hello world"}}
out: {"ok": true, "data": {"summary": "2 words", "words": 2}}
out: {"ok": false, "error": "No text was given to count."}
```

Anything else printed to stdout corrupts the protocol — send diagnostics to
stderr, which Jarvis shows if the plugin fails. A call that takes more than 20
seconds is killed.

`word-count/` is a working reference. Copy it.
