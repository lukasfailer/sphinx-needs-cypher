"""The commercial backend: shell out to ``ubc query cypher``.

This proves the second half of the split — the *same* directive can be powered
by useblocks' fast Rust engine when the binary is present and licensed, with no
change to the query the author wrote. It is the same language, executed by a
compiled engine:
identical query language, a real planner, incremental indexing. The reference
backend and this backend answer the same Cypher; the difference is speed and
scale — the same language, a different engine underneath.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class UbcNotAvailable(RuntimeError):
    pass


class UbcBackend:
    name = "ubc"

    def __init__(self, project_dir: str | Path, binary: str | None = None) -> None:
        self.project_dir = Path(project_dir)
        self.binary = binary or shutil.which("ubc")
        if not self.binary:
            raise UbcNotAvailable("`ubc` binary not found on PATH")

    def query(self, cypher: str) -> list[dict[str, Any]]:
        proc = subprocess.run(
            [self.binary, "query", "cypher", cypher, "--format", "json"],
            cwd=self.project_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise UbcNotAvailable(
                f"ubc query failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return self._parse(proc.stdout)

    def select_ids(self, cypher: str, var: str = "n") -> list[str]:
        rows = self.query(cypher)
        out: list[str] = []
        seen: set[str] = set()
        for row in rows:
            val = row.get(var) if isinstance(row, dict) else None
            if val is None and isinstance(row, dict):
                val = next(iter(row.values()), None)
            if isinstance(val, str) and val not in seen:
                seen.add(val)
                out.append(val)
        return out

    @staticmethod
    def _parse(stdout: str) -> list[dict[str, Any]]:
        stdout = stdout.strip()
        if not stdout:
            return []
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # Some ubc versions emit JSON lines; fall back to per-line parse.
            return [json.loads(line) for line in stdout.splitlines() if line.strip()]
        if isinstance(data, dict) and "rows" in data:
            return data["rows"]
        if isinstance(data, list):
            return data
        return [data]
