---
name: self-improvement
description: Use when the owner asks Zoe to learn something, to add, change, improve or remove one of her functions or behaviors, or when something she does falls short and she decides to improve herself. Explains how to change her own skills and code safely.
---

# Changing yourself

You can change your own skills and code, and this is how you learn: never work
around yourself with a separate script that does on its own what you should do,
or with a written rule standing in for a change to your code. Never edit live
files either: every change goes through the pipeline below, which checks it, has
it reviewed independently and keeps it reversible. Read `PRINCIPLES.md` in this
folder first and follow it.

A change takes time: build it, test it, review it. When it takes more than a few
minutes, hand it to a parallel mind whose brief says to use this skill, so the
chat stays free.

## 1. Choose the target

- `skills`: your private plugin. New capabilities made for this owner live there,
  one folder per skill under `skills/<name>/` with a `SKILL.md` and any scripts or
  tests it needs. A skill takes effect at the next turn, with no restart.
- `core`: your own code and the built-in skills, for how you work (receiving and
  sending messages and files, memory, permissions, voice). A core change takes
  effect after a graceful restart, done once no turn is waiting or running and no
  parallel mind is working.

## 2. Work in the change's own copy

    zoe-evolve start <target> <short-name>

It prints the folder to edit. Edit only inside that folder, and add or update
tests for every change in behavior.

## 3. Check and review

    zoe-evolve check <target> <short-name>
    zoe-evolve review <target> <short-name>

Fix what they report and run both again until they pass. Never weaken a test to
make it pass.

## 4. Apply

    zoe-evolve apply <target> <short-name> -m "<one line in English: what changes and why>"

Add `--owner-approved` only when the owner explicitly asked for a change to a
protected part listed in `PRINCIPLES.md`.

Then remove what the change replaces: a script, a tool or a written rule that
did the same job some other way.

## 5. Tell the owner

In one or two lines: what changed and why. The owner can undo it from the menu
(the Menu button, then the undo entry).

To abandon a change: `zoe-evolve discard <target> <short-name>`.
To see open and recent changes: `zoe-evolve status`.
