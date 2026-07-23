"""toolsigma — secverify at the 'sigma' capability level (strict / max review)."""
from .cli import main


def cli_entry() -> int:
    """Console-script entry point (the installed ``secverify`` command).

    Runs the strictest configuration: sigma capability level with strict
    review on by default, so every prose number and every unconfirmed
    in-table figure is reviewed.
    """
    return main(level="sigma", strict_default=True)


if __name__ == "__main__":
    raise SystemExit(cli_entry())
