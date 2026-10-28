## What changes

<!-- one or two lines: what the user notices -->

## Changelog

[...autoGenerate]

## Checklist

- [ ] `VERSION` bumped (merging into `main` creates the tag `vX.Y.Z` and the release)
- [ ] `./run-tests.sh` ends with `ALL GREEN` (`RUN_E2E=1` when the client or the server changed)
- [ ] Server coverage still at 100% and the new code has tests in the same PR
- [ ] New page text is English (`t("…")` or `data-i18n`) with its line in `PT_BR` in `web/i18n.js`
- [ ] New guardian notice text is English inside `tr()` with its line in `PT_BR` in `server/i18n.py`
- [ ] Changed the protocol, an `/api/*` route, a preset or an `LRD_*` variable? Updated `CLAUDE.md` and `README.md` (`tests/test_docs_sync.py` checks it)
- [ ] New protocol field is sanitized and has a fuzz test (bad input never drops the connection or the encoder)
- [ ] Changed `pyproject.toml`? Ran `uv lock` and exported both `requirements*.txt`
- [ ] Tested on a real device on the LAN when the client, the encoder or the input changed
