# CI fixture databases

`x.db` and `x_legacy_claude.db` are copies of `out/x.db` (live Playwright runs, 2026-09-06 to 09-23)
and `out/x_legacy_claude.db` (the 95-account baseline), taken on 2026-10-02. `out/` is git-ignored,
so CI copies these two files into `out/` before running pytest.

The `post_texts` table in `x.db` was emptied before committing: the tests need counts and
timestamps, not what people wrote. Everything else is unchanged.
