# Blast Radius entry

Test selector for the Builders Buildathon Blast Radius track. For each sqlglot change, it prints the tests that ran the changed code at the pinned commit `eb11aec4`, split across workers.

## Build and run

```sh
docker build -t blastradius .
docker run --rm --network none --read-only -v /path/to/data:/data:ro blastradius
```

The build downloads nothing. The image runs as any user and only reads.

## Files

- `selector.py`: reads `/data` and prints one JSON line per change. Anything it can't place (a new module, an unknown file, an error) runs every test.
- `map.json`, `map_ids.json`: which tests ran which functions, lines and classes, and which files each test read, recorded once by running sqlglot's test suite at the pinned commit. Not built from any patch.
- `attr_reads.json`: for each name assigned in a sqlglot class body, the tests that ran a line that could read it (`x.NAME`, a bare `NAME`, the string "NAME", or reflective access such as `getattr`, `vars`, `dir`, `__dict__`). Built from the pinned source and `map.json`, not from any patch.
- `sqlglot-src.tar.gz`: sqlglot's own `.py` files at the pinned commit, so each patch can be compared with the original code.
- `import_baseline.json` (made during the build): which sqlglot modules fail to import on the pinned source.

## How it selects

Each patch is applied to the pinned source, compiled (not run), and compared per function, class and statement. Changed code maps to the tests that ran it, and the tests are packed onto the cheapest worker count. Code that doesn't compile, module-level changes, metaclass methods and classes never seen instantiated select every importer. Other class-body changes import every sqlglot module with the patched files swapped in, and select every importer if anything new fails. A class-body change that only alters side-effect-free `NAME = value` assignments on a plain class narrows to the users that could read the changed names.

## Third-party code

- [sqlglot](https://github.com/tobymao/sqlglot) at commit `eb11aec4`, MIT licensed (see `SQLGLOT-LICENSE`). Its sources are shipped for comparison only.
- Python standard library only otherwise.

MIT licensed (see `LICENSE`).
