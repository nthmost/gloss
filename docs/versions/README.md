# Versions

Each iteration of gloss is *crystallized*: a markdown file here named for the
version, pointing at the commit that closes it, plus a matching git tag and
GitHub release. The file is the narrative (what it does, how it's built, what
was decided and why, what's still missing); the release is the pointer.

| version | date | doc | tag |
|---|---|---|---|
| 0.1 | 2026-11-24 | [v0.1.md](v0.1.md) | [`v0.1`](https://github.com/nthmost/gloss/releases/tag/v0.1) |

To crystallize the next one: write `vX.Y.md` (copy the header table from the
last), add a row here, commit, then

```sh
git tag -a vX.Y -m "gloss vX.Y" && git push origin vX.Y
gh release create vX.Y --title "gloss vX.Y" --notes-file docs/versions/vX.Y.md
```
