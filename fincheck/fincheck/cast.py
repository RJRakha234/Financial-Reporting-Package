"""Entry point so ``python -m fincheck.cast`` runs the casting CLI."""

from .casting_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
