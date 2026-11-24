"""Write frontend/tests/contract-examples.json from scripts/contract_examples.py.

Run after changing a contract; tests/test_contracts.py fails while the file is stale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'frontend' / 'tests' / 'contract-examples.json'


def render() -> str:
    sys.path.insert(0, str(ROOT))
    from scripts.contract_examples import examples  # noqa: PLC0415 — needs ROOT on sys.path first

    return json.dumps(examples(), ensure_ascii=False, indent=4) + '\n'  # prettier's own layout


if __name__ == '__main__':
    OUT.write_text(render(), encoding='utf-8')
    print(f'wrote {OUT.relative_to(ROOT)}')
