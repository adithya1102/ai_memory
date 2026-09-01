---
name: Bug report
about: Something behaves differently than it should
title: ''
labels: bug
assignees: ''
---

<!--
Before anything else: ContextVault holds private conversations. Please redact
anything personal from logs, titles and sample files before posting.
-->

## What happened

<!-- One or two sentences. -->

## Steps to reproduce

1.
2.
3.

## Expected vs actual

**Expected:**

**Actual:**

## Sample data

<!--
This is by far the most useful thing you can provide for import and parsing
bugs. Export formats vary in ways that are very hard to guess at from a
description.

If you can, attach a MINIMAL conversations.json that reproduces the problem —
one or two conversations, with the content replaced by dummy text but the
STRUCTURE left intact (the nesting, the field names, the nulls). The structure
is the part that matters; the words are not.
-->

## Environment

- **OS:**  <!-- e.g. Windows 11, macOS 14.2, Ubuntu 24.04 -->
- **Python:**  <!-- python --version -->
- **ContextVault version / commit:**  <!-- git rev-parse --short HEAD -->
- **Installed semantic extras?**  <!-- yes / no — pip show sentence-transformers sqlite-vec -->
- **Provider and export date:**  <!-- e.g. ChatGPT, exported 2026-08-14 -->

## Library size

<!--
From the Settings page. Some bugs only appear at scale, and knowing whether
this is 40 conversations or 4,000 narrows it down a lot.
-->

- Conversations:
- Messages:

## Test suite result

<!--
`python tests/run_all.py` — paste the summary lines at the bottom. If the
suite fails on a clean checkout, that alone is very useful to know, and often
points straight at an environment problem.
-->

```
```

## Logs

<!--
Run with `python backend/main.py --no-window` to see server output in your
terminal, and paste anything relevant. Check for anything private first.
-->

```
```
