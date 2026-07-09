"""toolbeta — secverify at the 'beta' capability level."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(level="beta"))
