# Changelog

## 0.1.0

First release.

- `--workers N` (or `auto`) runs tests in workers forked from the collecting process.
- `gc.freeze()` before the fork, so the garbage collector stops dirtying shared pages.
- Workers that die mid-test are reported against the test they were running and replaced;
  `--workers-max-restarts` caps that.
- `--workers-no-freeze` for measuring what the freeze is worth.
