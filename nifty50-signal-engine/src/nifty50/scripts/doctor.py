"""Check the setup and say exactly what is wrong with it.

    python -m nifty50.scripts.doctor

Written because the failure modes here are silent and the error messages
point at the wrong thing. A ``.env`` saved by Notepad carries a byte-order
mark that attaches to the first key in the file, so ``KITE_API_KEY`` goes
missing while every other line loads -- and the traceback says the variable
"is not set", which sounds like a typo in a value that is sitting there in
plain sight. Diagnosing that from a stack trace is not reasonable to expect
of anyone.

Every check names the file it looked at and the command that fixes it.
Values are never printed: this reports whether a credential is present and
plausible, never what it is, so the output is safe to paste into a chat.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from nifty50.config import Config, load_config, load_env, project_root, read_text_any_encoding

OK = "ok  "
WARN = "warn"
FAIL = "FAIL"

# A Kite access token is a short opaque string. Anything with angle brackets
# or whitespace in it is a placeholder that was pasted without being replaced.
_PLACEHOLDER_MARKS = ("<", ">", "your_", "paste", "the access_token", "...")


@dataclass(slots=True)
class Check:
    status: str
    name: str
    detail: str
    fix: str = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-broker", action="store_true",
        help="also contact Kite to confirm the token is live (needs network)",
    )
    args = parser.parse_args(argv)

    root = project_root()
    print(f"project root  {root}")
    print(f"python        {sys.version.split()[0]}  ({sys.executable})\n")

    checks: list[Check] = []
    checks.extend(_check_env_file(root))
    config = load_config()
    checks.extend(_check_credentials(config))
    checks.extend(_check_data(config))
    if args.check_broker:
        checks.append(_check_broker(config))

    width = max(len(check.name) for check in checks)
    for check in checks:
        print(f"  [{check.status}] {check.name:<{width}}  {check.detail}")
        if check.fix:
            for line in check.fix.splitlines():
                print(f"         {line}")

    failed = [check for check in checks if check.status == FAIL]
    print()
    if failed:
        print(f"{len(failed)} problem(s) to fix above.")
        return 1
    print("Setup looks good.")
    return 0


def _check_env_file(root: Path) -> list[Check]:
    checks: list[Check] = []
    env_path = root / ".env"

    # Notepad's "save as type: Text Documents" silently appends .txt, and the
    # result looks right in Explorer with extensions hidden.
    strays = sorted(p.name for p in root.glob(".env.*") if p.name != ".env.example")
    if strays:
        checks.append(
            Check(
                WARN, ".env.txt", f"found {', '.join(strays)}",
                fix="Notepad appended an extension. Rename it:\n"
                    f"  ren {strays[0]} .env",
            )
        )

    if not env_path.is_file():
        # Not a failure on its own. A .env is one way to supply credentials;
        # exported environment variables are another, and the only one CI
        # uses. The credential checks below decide whether anything is wrong.
        checks.append(
            Check(
                WARN, ".env exists", f"not found at {env_path}",
                fix="copy .env.example .env      (then edit it)\n"
                    "Skip this if the credentials come from the environment.",
            )
        )
        return checks

    raw = env_path.read_bytes()
    encoding = _describe_encoding(raw)
    checks.append(Check(OK, ".env exists", f"{env_path}  ({len(raw)} bytes, {encoding})"))

    # This used to break the first key in the file and nothing said so.
    if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        checks.append(
            Check(
                WARN, ".env encoding", f"{encoding} -- handled, but avoid it",
                fix="Notepad wrote a byte-order mark. It is read correctly now,\n"
                    "but 'Save as > Encoding: UTF-8' keeps other tools happy.",
            )
        )

    loaded = load_env(root)
    checks.append(
        Check(OK if loaded else FAIL, ".env loaded", "read into the environment")
    )
    return checks


def _describe_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return "UTF-8 with BOM"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "UTF-16"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8"
    return "UTF-8"


def _check_credentials(config: Config) -> list[Check]:
    kite = config.broker.kite
    checks: list[Check] = []

    for label, name, required in (
        ("KITE_API_KEY", kite.api_key_env, True),
        ("KITE_API_SECRET", kite.api_secret_env, True),
        ("KITE_ACCESS_TOKEN", kite.access_token_env, False),
    ):
        value = os.environ.get(name, "").strip()
        if not value:
            checks.append(
                Check(
                    FAIL if required else WARN, label, "empty or missing",
                    fix=_credential_fix(label),
                )
            )
        elif any(mark in value.lower() for mark in _PLACEHOLDER_MARKS):
            checks.append(
                Check(
                    FAIL, label, "still the placeholder text",
                    fix="Replace the whole value -- no angle brackets, no quotes:\n"
                        f"  {label}=your_real_value_here",
                )
            )
        else:
            checks.append(Check(OK, label, f"set ({len(value)} chars)"))

    request_token = os.environ.get(kite.request_token_env, "").strip()
    if request_token and not any(m in request_token.lower() for m in _PLACEHOLDER_MARKS):
        checks.append(
            Check(
                WARN, "KITE_REQUEST_TOKEN", "set -- will be consumed on the next run",
                fix="Request tokens are single-use. If a run already spent it,\n"
                    "blank this line or the next start reports 'token already used'.",
            )
        )

    if os.environ.get(kite.access_token_env, "").strip():
        checks.append(_check_token_age(config))
    return checks


def _credential_fix(label: str) -> str:
    if label == "KITE_ACCESS_TOKEN":
        return (
            "Not fatal -- the engine mints one from a request token. To reuse\n"
            "today's login instead, copy access_token out of .kite_session.json."
        )
    return (
        "Edit .env (beside config.yaml) and set it, then save:\n"
        f"  {label}=...        from https://developers.kite.trade/apps"
    )


def _check_token_age(config: Config) -> Check:
    """Kite tokens die at a wall-clock time, not after a duration."""
    from nifty50.domain import now_ist

    expiry_time = config.broker.kite.token_expiry_local_time
    now = now_ist()
    expiry = dt.datetime.combine(now.date(), expiry_time, tzinfo=now.tzinfo)
    if expiry <= now:
        expiry += dt.timedelta(days=1)
    hours = (expiry - now).total_seconds() / 3600
    if hours < 1:
        return Check(
            WARN, "token lifetime", f"expires in {hours * 60:.0f} min ({expiry:%d %b %H:%M})",
            fix="Kite issues no refresh token. Re-run the login before you need it.",
        )
    return Check(OK, "token lifetime", f"{hours:.1f}h left (expires {expiry:%d %b %H:%M} IST)")


def _check_data(config: Config) -> list[Check]:
    checks: list[Check] = []

    store = config.path(config.data.store.root)
    partitions = list(store.glob("exchange=*/symbol=*/timeframe=*")) if store.is_dir() else []
    checks.append(
        Check(
            OK if partitions else WARN, "bar store",
            f"{len(partitions)} series at {store}" if partitions else f"empty ({store})",
            fix="" if partitions else
                "python -m nifty50.scripts.backfill --symbols RELIANCE --timeframes 5m,15m",
        )
    )

    replay = config.path(config.broker.replay.root)
    series = list(replay.glob("*/*/*.parquet")) if replay.is_dir() else []
    checks.append(
        Check(
            OK if series else WARN, "replay root",
            f"{len(series)} series at {replay}" if series else f"empty ({replay})",
            fix="" if series else "python -m nifty50.scripts.build_replay --from-store",
        )
    )

    universe = config.path(config.universe.constituents_file)
    rows = 0
    if universe.is_file():
        # The shipped file is ~56 lines of explanation and no data. Counting
        # lines would report a populated universe and hide the one gap that
        # invalidates every cross-sectional result in the project.
        lines = [
            line for line in read_text_any_encoding(universe).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        rows = max(0, len(lines) - 1)  # less the header
    checks.append(
        Check(
            OK if rows else WARN, "index membership",
            f"{rows} row(s) in {universe.name}",
            fix="" if rows else
                "Point-in-time membership is unpopulated, so anything\n"
                "cross-sectional is survivorship-biased. Live charts are fine.",
        )
    )
    return checks


def _check_broker(config: Config) -> Check:
    """Actually call Kite. The only proof a token is live."""
    from nifty50.data.brokers.kite import KiteAdapter, KiteAuthError

    try:
        status = KiteAdapter(config).authenticate()
    except KiteAuthError as error:
        return Check(FAIL, "kite login", str(error).splitlines()[0], fix=str(error))
    except Exception as error:
        return Check(FAIL, "kite login", f"{type(error).__name__}: {error}")
    if not status.valid:
        return Check(FAIL, "kite login", status.message)
    return Check(OK, "kite login", "token accepted by the broker")


if __name__ == "__main__":
    raise SystemExit(main())
