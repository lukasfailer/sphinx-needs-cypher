# Data

`needs.ubc.json` is the graph both engines share. It is produced from
useblocks' public [`sphinx-needs-demo`](https://github.com/useblocks/sphinx-needs-demo)
with:

```bash
ubc build needs -o needs.ubc.json    # run inside the demo's docs/ directory
```

292 needs, 22 need types, 13 link types — a real ISO 26262 automotive safety
graph. Using `ubc`'s own export guarantees the reference engine and the `ubc`
engine index byte-for-byte the same nodes, so the parity check is apples-to-apples.
