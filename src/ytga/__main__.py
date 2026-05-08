"""Module entry point: GUI by default, `--cli` to fall through to CLI mode."""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from .cli import main as cli_main

        return cli_main(sys.argv[2:])

    from .app import main as gui_main

    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
