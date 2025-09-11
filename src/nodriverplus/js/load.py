import logging
from pathlib import Path

logger = logging.getLogger("nodriverplus.js.load")

def load_text(filename: str, encoding: str = "utf-8") -> str:
    # resolve path relative to this file to avoid relying on cwd
    base_dir = Path(__file__).resolve().parent
    path = base_dir.joinpath(filename)

    # log intent; let any IO errors propagate so callers can handle them
    logger.debug("loading text from %s", path)

    with path.open("r", encoding=encoding) as fh:
        return fh.read()