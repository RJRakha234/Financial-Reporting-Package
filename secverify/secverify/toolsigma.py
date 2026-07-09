"""toolsigma — secverify at the 'sigma' capability level."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(level="sigma"))
