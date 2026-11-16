#!/usr/bin/env python3
"""TEMPORARY diagnostic: compare a symbol's CRC across the ABI oracles that
bracket one GKI release.

Why it exists: a leg failed the ABI gate on `is_ashmem_file` at
android12-5.10-2023-09 even though ashmem.c is byte-identical to the next
release's. This tool pulls the ABI definition from the release before, the
release itself, and the release after, and prints the symbol's CRC in each -
so a stale oracle is distinguishable from a real divergence without trusting
any single tree's own file.

The ABI files are immutable per tag, so results are reproducible:
  android/abi_gki_aarch64.xml   (5.10 / 5.15 era, one crc per elf-symbol)
  gki/aarch64/abi.stg           (6.1+ era, crc: 0x... + name: "...")

Usage:
  abi_oracle_diff.py --family android12-5.10 --date 2023-09 --symbol is_ashmem_file
  abi_oracle_diff.py --family android12-5.10 --date 2023-09 --symbol is_ashmem_file \
      --symvers /path/to/Module.symvers

Delete this script once the oracle question it was added for is settled.
"""

import argparse
import base64
import re
import subprocess
import sys
import urllib.request

REPO = "https://android.googlesource.com/kernel/common"
GS = REPO + "/+/{ref}/{path}?format=TEXT"
PATHS = ("android/abi_gki_aarch64.xml", "gki/aarch64/abi.stg")


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def tags_for(branch):
    out = sh(f"git ls-remote --tags {REPO} 'refs/tags/{branch}_r*'")
    nums = []
    for line in out.splitlines():
        m = re.search(re.escape(branch) + r"_r(\d+)$", line.strip())
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else None


def tag_list(family):
    """{branch: highest r} for every release of one family."""
    out = sh(f"git ls-remote --tags {REPO} 'refs/tags/{family}-*'")
    best = {}
    for line in out.splitlines():
        m = re.search(r"refs/tags/(.+)_r(\d+)$", line.strip())
        if not m:
            continue
        branch, r = m.group(1), int(m.group(2))
        if not branch.startswith(family + "-"):
            continue
        rest = branch[len(family) + 1:]
        if not re.fullmatch(r"\d{4}-\d{2}", rest):
            continue
        best[branch] = max(best.get(branch, 0), r)
    return dict(sorted(best.items()))


def abi_text(tag):
    for path in PATHS:
        try:
            raw = urllib.request.urlopen(GS.format(ref=f"refs/tags/{tag}", path=path), timeout=90).read()
        except Exception:
            continue
        try:
            text = base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:
            continue
        if len(text) > 1000:
            return path, text
    return None, None


def crc_of(text, symbol):
    m = re.search(r"name='%s'[^>]*crc='(0x[0-9a-fA-F]+)'" % re.escape(symbol), text)
    if m:
        return m.group(1)
    m = re.search(r"crc:\s*(0x[0-9a-fA-F]+)\s*\n\s*name:\s*\"%s\"" % re.escape(symbol), text)
    if m:
        return m.group(1)
    m = re.search(r"name:\s*\"%s\"\s*\n\s*crc:\s*(0x[0-9a-fA-F]+)" % re.escape(symbol), text)
    return m.group(1) if m else None


def symvers_crc(path, symbol):
    if not path:
        return None
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            parts = line.split()
            if len(parts) >= 2 and parts[1] == symbol:
                return parts[0] if parts[0].startswith("0x") else "0x" + parts[0]
    except Exception as exc:
        print(f"  (could not read {path}: {exc})")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True, help="e.g. android12-5.10")
    ap.add_argument("--date", required=True, help="e.g. 2023-09")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--symvers", default=None, help="Module.symvers from the failing build")
    args = ap.parse_args()

    tags = tag_list(args.family)
    want = f"{args.family}-{args.date}"
    if want not in tags:
        print(f"no tags found for {want}", file=sys.stderr)
        return 1
    names = list(tags)
    idx = names.index(want)
    window = names[max(0, idx - 1): idx + 2]

    print(f"symbol: {args.symbol}   family: {args.family}   release: {args.date}")
    print(f"{'release':<28} {'tag':<10} {'abi file':<32} crc")
    got = {}
    for branch in window:
        tag = f"{branch}_r{tags[branch]}"
        path, text = abi_text(tag)
        crc = crc_of(text, args.symbol) if text else None
        got[branch] = crc
        mark = " <== this leg" if branch == want else ""
        print(f"{branch:<28} {tag.split('_r')[-1]:<10} {(path or '-'):<32} {crc or 'absent'}{mark}")

    built = symvers_crc(args.symvers, args.symbol)
    if built:
        print(f"\nthis build's Module.symvers: {built}")
        matches = [b for b, c in got.items() if c == built]
        print("matches oracle of: " + (", ".join(matches) if matches else "none"))

    vals = {c for c in got.values() if c}
    if len(vals) > 1:
        print("\nThe bracketing releases disagree on this symbol, and the source")
        print("file is unchanged between them - that is an oracle/toolchain change,")
        print("not a code change in the tree that failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
