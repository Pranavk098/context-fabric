# Release checklist: v0.1.1-alpha.1

**For a human.** These are the exact ordered steps the maintainer runs. An agent must not tag, push or publish.

The tag is `v0.1.1-alpha.1` because `v0.1.0` already exists and `v0.1.0-alpha.1` would sort before it.

## 0. Authenticate as the releasing account
```bash
gh auth status
gh auth switch --user Pranavk098     # do this first
gh auth status                       # must show Pranavk098 as active
```

## 1. Start from main, clean
```bash
git switch main
git pull --ff-only origin main
git status --short                   # must be empty
git log --oneline -1
```

## 2. Confirm the version is consistent
```bash
grep '^version' pyproject.toml       # 0.1.1a1
grep '__version__' cfxd/__init__.py  # 0.1.1a1
grep '"version"' .claude-plugin/plugin.json .claude-plugin/marketplace.json
git tag --list 'v0.1.1-alpha.1'      # must print nothing yet
```

## 3. Run the gate on that commit
```bash
uv run --python 3.12 python -m pytest -q \
  && uv run ruff check . \
  && uv run ruff format --check . \
  && uv run python -m mypy cfxd \
  && uv run python scripts/check_docs_numbers.py
```

## 4. Fresh-clone test (the install path, no local state)
```bash
git clone https://github.com/Pranavk098/context-fabric.git /tmp/cf-fresh
cd /tmp/cf-fresh
uv sync --locked
uv run python -m pytest -q
uv build
uv run cfxd --help
# Windows: the hook scripts need sh from Git for Windows
sh -c "echo ok"
```
Delete `/tmp/cf-fresh` afterwards.

## 5. Check CI is green on all three OSes
```bash
gh run list --branch main --limit 10
gh run view <run-id>                 # confirm ubuntu, macOS and Windows jobs passed
```
Do not tag a commit whose CI is red or still running.

## 6. Tag and push
```bash
git tag -a v0.1.1-alpha.1 -m "Context Fabric v0.1.1-alpha.1 (experimental; saving unproven)"
git push origin v0.1.1-alpha.1
```

## 7. Watch the release workflow
```bash
gh run list --workflow release.yml --limit 3
gh run watch <run-id>
```
It must build the sdist and wheel, write `SHA256SUMS`, attest build provenance, and create a **pre-release**
using `docs/release-notes/v0.1.1-alpha.1.md`.

## 8. Verify the release page
```bash
gh release view v0.1.1-alpha.1
gh release download v0.1.1-alpha.1 --pattern 'SHA256SUMS' --dir /tmp/cf-rel
gh attestation verify /tmp/cf-rel/context_fabric-0.1.1a1-py3-none-any.whl \
  --repo Pranavk098/context-fabric
```
Check by eye: the page is marked *Pre-release*, the notes are the v0.1.1 text (not a fallback line), and all
four assets are attached.

## 9. Decide what to do with the old release
`v0.1.0` is the pre-plugin kernel milestone and stays valid. Leave it published, or retitle its release page
(for example `v0.1.0 — kernel milestone (superseded by v0.1.1-alpha.1)`) so testers do not install the wrong
thing. Do not delete or move the `v0.1.0` tag.

## 10. Post-release smoke test
```bash
claude plugin marketplace add Pranavk098/context-fabric#v0.1.1-alpha.1
claude plugin install context-fabric@context-fabric
uvx --from git+https://github.com/Pranavk098/context-fabric@v0.1.1-alpha.1 cfxd doctor
```
Confirm `cfxd doctor: healthy` (including `sh: found` on Windows) before announcing.
