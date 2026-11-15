# How `stable` is chosen

Not by hand-picking versions and testing them. The set is **derived by a
resolver**, and this is the whole process. The design rationale is in
[design.md §7](design.md#7-the-stable-solve).

## Inputs

```text
the library names in registry/*.toml
+ haywire-core, haywire-studio, nicegui  pinned to the target Haywire release
+ stable.constraints.txt                 the curator's holds and exclusions
+ --universal                            resolve across all platforms at once
+ --exclude-newer <timestamp>            freeze the view of PyPI
```

## The solve

`scripts/solve_stable.py` runs to completion on its own. It asks nothing, picks
no versions by hand, and produces the same answer every time for the same
inputs.

It works by giving the resolver an objective it does not have on its own. Every
library gets two bounds:

- a **hard floor** at its current stable pin, so the set can never go
  backwards;
- an **aspiration** at its newest release.

It resolves. If everything fits, that is the answer and the run is over. If it
does not, the resolver's error names the libraries in the clash, and the solve
relaxes exactly one of their aspirations back to its hard floor, records why,
and resolves again. Two or three passes settle it; each pass is one resolver
call, and only the libraries the resolver actually named are ever candidates.

**Which one gets relaxed** is decided in this order:

1. Whichever relaxation leaves **the most libraries at their newest version**.
2. On a tie, the **more recently released** one. The rest of the set was
   working; the new release is what broke the fit, and its author is the
   person who can widen a constraint. It is also self-correcting: next cycle it
   is no longer the newest.
3. Unless `stable.constraints.txt` says otherwise, which always wins.

Every relaxation lands in `stable-report.md` with the resolver's own
explanation and what taking the newer version would have cost.

## Overriding the outcome

The solve is local. Add a line to `stable.constraints.txt`
(`haybale-visiongraph<2.0`, say), re-run, and read the diff of
`locks/stable.toml`: every library whose version moved as a consequence. That
diff is the answer to "what does holding this back cost us". Discard it and try
something else; nothing is pushed until it is right.

You should not normally need to. In an ordinary cycle the solve runs, the lock
diff is the answer, and the constraints file is untouched.

## Verification

The chosen lock and the constraints change go into one pull request. CI then:

- **recompiles from scratch** and fails if the committed lock is not what those
  inputs produce. This is why the resolution is timestamp-frozen: CI and the
  curator see the same PyPI;
- **installs and imports** every pinned version, in a job with no permissions
  and no secrets;
- **checks PyPI for yanked releases**, because an exact `==` pin installs a
  yanked version anyway. Yanking is the one safety signal our own pinning
  defeats, so it is checked explicitly.

Only then is the lock merged, tagged, and deployed.
