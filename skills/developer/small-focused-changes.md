# Small, Focused Changes

## Rule (non-negotiable)

Every edit addresses ONE concern. One bug fix, one feature addition, one refactor. Do not combine unrelated changes. Do not "clean up" code while fixing a bug. Do not add features while refactoring.

## When This Fires

- Every code edit
- Every commit
- Any time you feel tempted to "also fix this while I'm here"

## The Discipline

1. **Identify the single concern** — What is the one thing this edit accomplishes?
2. **Touch only what's necessary** — If a line doesn't need to change for this concern, don't touch it.
3. **Match existing style** — Do not reformat, rename, or restructure code that isn't part of your change.
4. **Commit the concern** — One concern per commit. The commit message should describe one thing.

## Scope Boundaries

| Acceptable | Not acceptable |
|---|---|
| Fix the bug | Fix the bug AND reformat the file |
| Add the feature | Add the feature AND refactor the helper function |
| Rename the variable (if that's the task) | Rename the variable AND add type hints to the whole file |
| Update the import | Update the import AND remove "unused" imports you noticed |

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "While I'm here, I should also fix this." | File a separate task for it. Don't mix concerns. |
| "This code is ugly, let me clean it up." | Cosmetic changes obscure functional changes in diffs. Separate commits. |
| "It's faster to do both at once." | Faster to write, harder to review, harder to revert. Separate them. |
| "These changes are related." | Related changes can still be separate commits. Keep them focused. |
| "No one will notice the extra cleanup." | Reviewers notice. Bisect notices. Keep it focused. |

## Red Flags (self-check)

- Your diff touches files unrelated to the stated task
- Your commit message needs "and" to describe what changed
- You reformatted or restyled code you didn't functionally change
- You added comments, docstrings, or type hints to code you didn't change
- Your change is over 100 lines and the task was a "small fix"

## Hard Gate

Commits that mix unrelated concerns are invalid. If you catch yourself combining changes, split them into separate commits before proceeding.
