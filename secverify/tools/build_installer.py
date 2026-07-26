"""Build the two delivery artefacts from the working tree.

    python tools/build_installer.py [outdir]

Writes ``secverify.zip`` and ``secverify_install.py``.  The installer is a
single text file with the zip embedded as base64, so it survives transports
that only carry text (chat, ticket, email body) — which is how this tool
actually reaches reviewers.

Packaging was done by hand until now, and a hand-built package is exactly how a
stale copy gets delivered: the one failure mode that makes an already-fixed
defect reappear in front of a reviewer.  So this script derives the file list
from the tree, refuses to build unless the tests pass, and stamps the test
count into RUN.txt rather than trusting the number written there.
"""

from __future__ import annotations

import base64
import pathlib
import re
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: shipped as-is; anything not listed here is not delivered
INCLUDE = [
    "LIMITATIONS.md",
    "README.md",
    "RUN.txt",
    "pyproject.toml",
    "requirements.txt",
]
INCLUDE_GLOBS = ["secverify/*.py", "tests/test_*.py", "tools/*.py"]

_HEADER = '''"""SECVERIFY self-extracting installer.

Everything the tool needs is embedded in this one file, so it survives any
transport that only allows text.  Nothing is downloaded and nothing is executed
from the payload during extraction -- it is a plain zip written to disk.

    1. save this file as  secverify_install.py
    2. python secverify_install.py
    3. cd secverify  &&  pip install -r requirements.txt
    4. python -m secverify.toolsigma auditorsreport.pdf statement.pdf exhibit.htm

Pass a target directory to extract somewhere other than ./secverify :

    python secverify_install.py D:\\\\tools

Extraction refuses to overwrite an existing folder unless you add --force, so a
previous install is never silently replaced.  KEEP ONE FOLDER: running an older
extraction is the one way to see a defect that has already been fixed.  Confirm
with "python -m pytest -q" -- expect {ntests} passed.

Read RUN.txt for the three things that matter most, and LIMITATIONS.md before
relying on the output.
"""

import base64
import io
import pathlib
import sys
import zipfile

PAYLOAD = """\\
{payload}"""


def main(argv):
    force = "--force" in argv
    args = [a for a in argv if not a.startswith("--")]
    target = pathlib.Path(args[0] if args else ".") / "secverify"

    if target.exists() and not force:
        print(f"refusing to overwrite existing {{target}} -- move it aside, "
              f"choose another location, or re-run with --force")
        return 1

    data = base64.b64decode("".join(PAYLOAD.split()))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        bad = [n for n in z.namelist() if n.startswith(("/", "\\\\")) or ".." in n]
        if bad:
            print(f"payload contains unsafe paths, refusing: {{bad[:3]}}")
            return 1
        target.mkdir(parents=True, exist_ok=True)
        z.extractall(target)
        n = len(z.namelist())

    print(f"extracted {{n}} files to {{target.resolve()}}")
    print()
    print("next:")
    print(f"    cd {{target}}")
    print("    pip install -r requirements.txt")
    print("    python -m pytest -q            (expect {ntests} passed)")
    print("    python -m secverify.toolsigma auditorsreport.pdf statement.pdf exhibit.htm")
    print()
    print("read RUN.txt first, and LIMITATIONS.md before relying on the output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


def run_tests() -> int:
    """Return the passing test count, or exit if anything fails."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    m = re.search(r"(\d+) passed", proc.stdout)
    if proc.returncode != 0 or not m:
        sys.stderr.write(proc.stdout[-3000:] + proc.stderr[-2000:])
        sys.exit("REFUSING TO BUILD — the test suite does not pass.")
    if "failed" in proc.stdout or "error" in proc.stdout.lower():
        sys.exit(f"REFUSING TO BUILD — {proc.stdout.strip().splitlines()[-1]}")
    return int(m.group(1))


def stamp_run_txt(ntests: int) -> None:
    """Keep the promised test count in RUN.txt true to what actually passed."""
    path = ROOT / "RUN.txt"
    text = path.read_text()
    fixed = re.sub(r"\b\d+(?= passed\))", str(ntests), text)
    fixed = re.sub(r"\b\d+(?= tests; run from this folder\))", str(ntests), fixed)
    if fixed != text:
        path.write_text(fixed)
        print(f"RUN.txt: test count stamped to {ntests}")


def collect() -> list[pathlib.Path]:
    files = [ROOT / name for name in INCLUDE]
    for pattern in INCLUDE_GLOBS:
        files.extend(sorted(ROOT.glob(pattern)))
    missing = [f for f in files if not f.is_file()]
    if missing:
        sys.exit(f"missing expected file(s): {[str(m) for m in missing]}")
    return files


def main(argv: list[str]) -> int:
    outdir = pathlib.Path(argv[0]) if argv else ROOT.parent
    outdir.mkdir(parents=True, exist_ok=True)

    ntests = run_tests()
    print(f"tests: {ntests} passed")
    stamp_run_txt(ntests)

    files = collect()

    # Two layouts, deliberately different.  The standalone zip is PREFIXED with
    # "secverify/" so unzipping it anywhere produces one folder rather than
    # scattering 40 files into the current directory.  The installer's payload
    # must NOT be prefixed, because the installer creates the target folder
    # itself and then extracts into it — prefixing both gives the reviewer a
    # doubled secverify/secverify/ and a tool that will not import.
    zip_path = outdir / "secverify.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f"secverify/{f.relative_to(ROOT).as_posix()}")
    print(f"{zip_path}  ({zip_path.stat().st_size:,} bytes, {len(files)} files)")

    inner = __import__("io").BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(ROOT).as_posix())
    payload = base64.b64encode(inner.getvalue()).decode()
    wrapped = "\n".join(payload[i:i + 128] for i in range(0, len(payload), 128))
    installer = outdir / "secverify_install.py"
    installer.write_text(_HEADER.format(payload=wrapped, ntests=ntests))
    print(f"{installer}  ({installer.stat().st_size:,} bytes)")

    # A built installer that cannot extract is worse than none, so it is run
    # for real into a scratch directory and the extracted copy is imported and
    # tested.  Checking only that the zip is readable is not enough: it passes
    # happily on a payload that extracts to secverify/secverify/, where nothing
    # imports at all.
    verify_installer(installer, ntests)
    return 0


def verify_installer(installer: pathlib.Path, ntests: int) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        proc = subprocess.run(
            [sys.executable, str(installer), str(tmp)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            sys.exit(f"installer failed to extract:\n{proc.stdout}{proc.stderr}")
        target = tmp / "secverify"
        pkg = target / "secverify" / "__init__.py"
        if not pkg.is_file():
            found = sorted(p.name for p in target.iterdir())
            sys.exit(
                f"installer laid out the wrong tree — expected {pkg} "
                f"(is the payload double-prefixed?); {target} contains {found}"
            )
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=target, capture_output=True, text=True,
        )
        m = re.search(r"(\d+) passed", proc.stdout)
        if proc.returncode != 0 or not m:
            sys.exit(
                "the EXTRACTED copy does not pass its tests:\n"
                + proc.stdout[-3000:] + proc.stderr[-1500:]
            )
        if int(m.group(1)) != ntests:
            sys.exit(
                f"extracted copy ran {m.group(1)} tests, the tree ran {ntests} "
                "— the package is missing test files"
            )
        proc = subprocess.run(
            [sys.executable, "-m", "secverify.toolsigma", "--help"],
            cwd=target, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            sys.exit(f"extracted copy will not run:\n{proc.stdout}{proc.stderr}")
    print(f"verified: installer extracts, imports, and passes {ntests} tests")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
