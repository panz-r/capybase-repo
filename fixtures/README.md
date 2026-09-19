# capybase rebase fixtures

Deterministic, script-built merge-conflict fixtures. There is **no
submodule and no downloaded data**: each fixture is a declarative JSON
spec, and `build.py` turns specs into small real git repos. Reproducible
fixtures are part of the product — the same spec builds byte-identical
commits (identical OIDs) on any machine.

## Layout

- `specs/<id>.json` — one self-contained fixture per file
- `build.py` — the deterministic builder (stdlib only)
- `.built/<id>/` — generated repos (gitignored; rebuilt on demand)

## Spec format

Every fixture specifies its base, both branches, the expected developer
resolution, acceptable alternatives, and validation commands:

| field                    | meaning                                                        |
|--------------------------|----------------------------------------------------------------|
| `id`, `title`, `notes`   | identity + what the fixture exercises                          |
| `path`, `language`       | the conflicted file and its language                           |
| `base`                   | common-ancestor content of `path`                              |
| `current`                | rebase-target side (git "ours", the upstream branch)           |
| `replayed`               | the branch being rebased (git "theirs")                        |
| `expected_resolved`      | the preferred developer resolution                             |
| `acceptable_alternatives`| other resolutions a developer could defend                     |
| `expected_conflict_hunks`| conflict markers a plain `git rebase` must produce             |
| `validation`             | commands that must pass on a resolution (`{python}` → interpreter, `{tmpdir}` → scratch dir) |

Branch names inside a built repo: `base`, `current`, `replayed`.

## Build

```bash
python fixtures/build.py --all          # every spec (idempotent by spec sha)
python fixtures/build.py --spec python-uu --force
```

## Reproduce a conflict

```bash
cd fixtures/.built/python-uu
git checkout replayed
git rebase current        # stops on the UU conflict
# from the capybase repo root:
capybase --repo "$PWD" run
git rebase --abort        # reset after a run
```

`scripts/run-live-test.sh python-uu` does all of the above (rebuild
included) around a live capybase run.

## Fixtures

| id              | file            | language | hunks | what it exercises                                             |
|-----------------|-----------------|----------|-------|---------------------------------------------------------------|
| text-uu-simple  | story.txt       | text     | 1     | both sides rewrite the same line's words; preferred merge combines both edits |
| python-uu       | app.py          | python   | 1     | both-modify-same-line; preferred merge synthesizes both return values |
| settings-uu     | settings.py     | python   | 2     | multi-hunk: both-sides-add combine + feature-flag flips, f-string (literal braces) auto-merged between hunks |
| rust-uu         | src/config.rs   | rust     | 2     | `impl`-block: new struct field on one side, field value + format-string changes on both |

The `expected_resolved` / `acceptable_alternatives` contents double as
oracle material: a resolver's output may equal the preferred resolution
or any listed alternative. `tests/test_fixture_builder.py` pins all of
this — spec completeness, builder determinism, conflict reproduction,
and resolution validity (each alternative rebases to completion and
passes the spec's validation commands).
