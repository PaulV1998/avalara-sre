"""Process entry point for ``python -m slo_eval`` and the zipapp artifact."""

import sys
from typing import NoReturn

from slo_eval.cli import main


def run() -> NoReturn:
    # zipapp's generated launcher ignores the return value of its entry function,
    # so the exit code is raised here to keep it visible to automation.
    sys.exit(main())


if __name__ == "__main__":
    run()
