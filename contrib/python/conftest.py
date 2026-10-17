"""Give synthetic subprocess tests the same explicit source paths as pytest."""
from pathlib import Path
import os

_ROOT = Path(__file__).resolve().parent
_TEST_PATHS = [str(_ROOT / path) for path in ('src/onec-odata-mcp/tests', 'tools/bank-graph', 'onec_bank_graph', 'onec_bank_graph/scripts')]
os.environ['PYTHONPATH'] = os.pathsep.join(_TEST_PATHS + [os.environ.get('PYTHONPATH', '')])
