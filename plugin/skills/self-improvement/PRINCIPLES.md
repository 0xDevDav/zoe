# Principles for changing Zoe

Zoe can change her own code and skills. These principles keep her growing like a
brain rather than like a pile of patches. The review step judges every change
against them.

## Where a change belongs

- A capability, meaning a task Zoe can perform, a source she can read or a report
  she can produce, is a skill. Skills made for this installation go in the private
  plugin; only generic skills with nothing personal in them belong to the
  built-in plugin.
- How Zoe works (receiving messages, memory, permissions, formatting, voice) is
  the core.
- One concern, one place. Before writing code, find the module or skill that
  already owns the concern and change it there.

## How to change

- Fix causes, not symptoms. A special case for one message, one person or one
  date is a patch: find the rule that makes the case work.
- Prefer deleting and simplifying to adding. A change that removes code while
  keeping the behavior is the best kind.
- No duplication: reuse, or move shared logic to the one place both callers use.
- Remove what is no longer used: dead code, unused options, skills that stopped
  being useful.
- Every change in behavior comes with a test that fails without it.
- The database only grows: a change adds tables and columns and never renames,
  drops or repurposes them, so an older version still runs on a newer database
  after a rollback.
- Small, complete changes: one purpose per change, nothing left half done.

## How the result reads

- English code with clear names. Comments only where the reason is not obvious,
  written in the present tense, never about earlier versions, fixes or history.
- Commit messages in English too: one line saying what changes and why.
- Documentation describes how things are now.
- No personal data in the core: it stays in ZOE_HOME and the private plugin.
- Tests use made-up examples (people such as Alex or Mia, neutral places, things
  and companies), never a name, place, business or fact from the owner's life,
  their conversations or Zoe's memory.

## What Zoe does not change on her own

Only an explicit request from the owner allows changes to the launcher, the
change pipeline, access and permissions, the vault, the privacy guard and its
hooks, their tests, and these principles.
