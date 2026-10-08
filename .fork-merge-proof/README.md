# Disposable fork merge fixture

This fixture is confined to `radical/aspire` and the disposable base
`fork-merge-proof-base-18213f39`. It must not land in the default branch or
upstream. It is not production Aspire code.

Run the behavior tests from the repository root:

```shell
python3 -B -m unittest discover -s .fork-merge-proof -p 'test_*.py' -v
```

`canonical_service` trims surrounding whitespace, lowercases service names, and
maps known aliases. Unknown names remain normalized. Empty or whitespace-only
names normalize to an empty string.

The dedicated workflow checks the exact published head on pushes and pull
requests. Its real GitHub Actions context, `Fork merge fixture 18213f39`, is
required with strict up-to-date checking only on the disposable base. This
fork-only lane does not use Aspire's conditional test selector or need a
production trigger-map entry.

Keep tests and the required check intact during repairs. Only repair actions
are enabled initially. Merge requires separate approval for the exact fork PR
and disposable base after fresh current-head checks and review resolution.
Never merge to `main` or upstream.
