# Read Before Modify

## Rule (non-negotiable)

Before modifying ANY code, you MUST read the existing implementation and understand the patterns in use. Never edit a file you haven't read. Never propose a change without understanding what it will affect.

## When This Fires

- Any request to modify, fix, or refactor code
- Bug investigation and fixing
- Adding new features that touch existing files
- Any code review or change suggestion

## The Read Protocol

Before making any code change:

1. **Read the target file** — Read the full file (or relevant section for large files). Do not rely on memory or assumptions about what the file contains.
2. **Trace the call chain** — Grep for all callers of the function you're changing. Check imports. Follow the data flow.
3. **Identify the blast radius** — List every file and function that will be affected by your change.
4. **Understand the patterns** — Note the conventions used in the file (naming, error handling, logging, structure). Your change must match.
5. **Only THEN plan the edit** — With full context, plan the minimum change needed.

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "I know what this file does." | Memory drifts. Read it fresh. |
| "It's a one-line fix." | One-line fixes have the highest regression rate. Read the context. |
| "I read this file earlier in the session." | Earlier reads are stale after other edits. Re-read. |
| "I'll read it after I make the change." | Reading after changing is debugging, not development. Read first. |
| "The user described exactly what to change." | Users describe intent, not implementation. Read the code. |

## Red Flags (self-check)

- You are about to edit a file you haven't read in this turn
- You are guessing at function signatures, variable names, or import paths
- You don't know what else calls the function you're modifying
- Your edit introduces a pattern that doesn't exist elsewhere in the file
- You are writing code that duplicates existing utility functions you haven't checked for

## Hard Gate

Any code edit is BLOCKED until the target file has been read and the blast radius assessed. Edits to unread files are invalid.
