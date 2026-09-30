#!/usr/bin/env python
"""Release candidate \u6821\u9a8c\uff08#26 / \u00a737\uff1a\u672c\u5730\u3001\u786e\u5b9a\u6027\u3001\u65e0\u7f51\u7edc\u3001\u4e0d\u89e6 Jev\uff09\u3002

\u804c\u8d23\uff08\u6545\u610f\u4fdd\u6301\u5c0f\u800c\u786e\u5b9a\uff0c\u4e0d\u505a release framework\uff09\uff1a

1. VERSION \u662f\u542b\u6cd5 SemVer \u4e09\u6bb5\u5f0f\uff1b
2. ZIP \u540d\u79f0\u4e0e VERSION \u4e00\u81f4\uff08SignalLens-v<version>-Windows-x64-portable.zip\uff09\uff1b
3. ZIP \u5185\u5fc5\u9700\u6587\u4ef6\u5b58\u5728\uff08SignalLens.exe / _internal / README-\u542f\u52a8\u8bf4\u660e.txt / LICENSE / VERSION\uff09\uff1b
4. ZIP \u5185\u7981\u6b62\u7528\u6237\u6570\u636e / \u79d8\u94a5 / \u7814\u7a76\u4ea7\u7269\uff08.env / settings.env / *.db / logs /
   runtime.json / evaluation reports / .git \u2026\uff09\uff1b
5. \u5305\u5185 VERSION \u4e0e\u4ed3\u5e93 VERSION \u4e00\u81f4\uff1b
6. .sha256 sidecar \u4e0e\u5b9e\u9645\u91cd\u7b97\u4e00\u81f4\uff1b
7. \u538b\u7f29\u5305\u5185\u4e0d\u51fa\u73b0\u7edD\u5bf9\u8def\u5f84\u6cc4\u9732\uff08workspace / \u7528\u6237\u540d\uff09\u3002

\u7528\u6cd5::

    python scripts/validate_release.py                       # \u9ed8\u8ba4 shipments/SignalLens-v<version>-*.zip
    python scripts/validate_release.py --zip path.zip [--expect-version 0.4.0]

\u9000\u51fa\u7801\uff1a0 = \u5168\u90e8\u901a\u8fc7\uff1b2 = \u9a8c\u8bc1\u5931\u8d25\uff08\u6253\u5370\u539f\u56e0\uff09\u3002
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_MEMBERS = (
    "SignalLens/SignalLens.exe",
    "SignalLens/_internal",
    "SignalLens/README-启动说明.txt",
    "SignalLens/LICENSE",
    "SignalLens/VERSION",
)

# \u7981\u6b62\u9879\uff1a\u7528\u6237\u6570\u636e / \u79d8\u94a5 / \u7814\u7a76\u4ea7\u7269 / \u7248\u672c\u63a7\u5236
BANNED_PATTERNS = (
    r"(^|/)\.env$",
    r"(^|/)settings\.env$",
    r"(^|/)\.git(/|$)",
    r"\.db$",
    r"\.db-wal$",
    r"\.db-shm$",
    r"\.sqlite3$",
    r"friend_history\.db",
    r"^SignalLens/data(/|$)",
    r"(^|/)logs(/|$)",
    r"(^|/)runtime\.json$",
    r"(^|/)evaluation/reports(/|$)",
    r"(^|/)\.release-test(/|$)",
    r"(^|/)dist(/|$)",
    r"(^|/)build(/|$)",
    r"\.pyc$",
)

# \u538b\u7f29\u5305\u5185\u4e0d\u5f97\u51fa\u73b0\u7684\u672c\u673a\u8def\u5f84\u7247\u6bb5\uff08\u9632\u6cc4\u9732\uff09
PATH_LEAK_MARKERS = (
    "/Users/",
    "\\Users\\",
    "D:\\Documents\\",
    "C:\\Users\\",
    "SIGNALLENS_DATA_DIR=",
    "TYPESAFE_API_KEY=",
)

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _repo_version() -> str:
    text = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not SEMVER_RE.match(text):
        raise SystemExit(f"VERSION \u4e0d\u662f SemVer \u4e09\u6bb5\u5f0f\uff1a{text!r}")
    return text


def _expected_zip_name(version: str) -> str:
    return f"SignalLens-v{version}-Windows-x64-portable.zip"


def _find_zip(version: str) -> Path:
    candidate = ROOT / _expected_zip_name(version)
    if candidate.exists():
        return candidate
    found = sorted(ROOT.glob(f"SignalLens-v{version}-Windows-x64-portable.zip"))
    if found:
        return found[0]
    raise SystemExit(f"\u627e\u4e0d\u5230 {candidate.name}\uff08\u8bf7\u5148\u8fd0\u884c "
                     f"scripts/package_portable.py\uff09")


def validate(zip_path: Path, expect_version: str | None) -> list[str]:
    errors: list[str] = []
    repo_version = _repo_version()
    version = expect_version or repo_version

    expected_name = _expected_zip_name(version)
    if zip_path.name != expected_name:
        errors.append(f"ZIP \u540d\u79f0 {zip_path.name!r} \u4e0e VERSION \u63a8\u5bfc\u7684 "
                     f"{expected_name!r} \u4e0d\u4e00\u81f4")

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

        for required in REQUIRED_MEMBERS:
            if not any(n == required or n.startswith(required)
                       for n in names):
                errors.append(f"\u7f3a\u5c11\u5fc5\u9700\u9879\uff1a{required}")

        for pattern in BANNED_PATTERNS:
            hits = [n for n in names if re.search(pattern, n)]
            if hits:
                errors.append(f"\u542f\u7981\u9879\u547d\u4e2d /{pattern}/\uff1a{hits[:5]}")

        for marker in PATH_LEAK_MARKERS:
            for n in names:
                # 只扫打包根的项目自有文件；_internal 属第三方库（其中构建机路径片段属上游产物，不归本项目数据面）。
                if n.startswith('SignalLens/_internal/'):
                    continue
                if n.endswith(('.py', '.pyc', '.json', '.txt', '.html')):
                    try:
                        blob = zf.read(n)
                    except Exception:
                        continue
                    if marker.encode("utf-8") in blob:
                        errors.append(f"{n} \u5305\u542b\u8def\u5f84/\u5bc6\u94a5\u7247\u6bb5\uff1a{marker}")
                        break

        bundled_version = None
        for n in names:
            if n.endswith("/VERSION") or n == "SignalLens/VERSION":
                bundled_version = zf.read(n).decode("utf-8").strip()
                break
        if bundled_version is None:
            errors.append("ZIP \u5185\u627e\u4e0d\u5230 VERSION")
        elif bundled_version != version:
            errors.append(f"\u5305\u5185 VERSION {bundled_version!r} != \u9884\u671f "
                         f"{version!r}")

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sidecar = zip_path.with_name(zip_path.name + ".sha256")
    if not sidecar.exists():
        errors.append(f"\u7f3a\u5c11 .sha256 sidecar\uff1a{sidecar.name}")
    else:
        text = sidecar.read_text(encoding="utf-8")
        if digest not in text:
            errors.append("sidecar SHA256 \u4e0e\u5b9e\u9645\u91cd\u7b97\u4e0d\u4e00\u81f4")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a SignalLens portable release candidate ZIP")
    parser.add_argument("--zip", default=None)
    parser.add_argument("--expect-version", default=None)
    args = parser.parse_args(argv)

    version = args.expect_version or _repo_version()
    zip_path = Path(args.zip) if args.zip else _find_zip(version)
    if not Path(zip_path).exists():
        print(f"validate error: ZIP \u4e0d\u5b58\u5728 {zip_path}", file=sys.stderr)
        return 2

    errors = validate(Path(zip_path), args.expect_version)
    size_mb = Path(zip_path).stat().st_size / 1024 / 1024
    digest = hashlib.sha256(Path(zip_path).read_bytes()).hexdigest()
    if errors:
        print(f"validate FAILED: {zip_path.name} ({size_mb:.1f} MB)")
        for err in errors:
            print(f"  - {err}")
        return 2
    print(f"validate OK: {zip_path.name} ({size_mb:.1f} MB)")
    print(f"  version: {version}")
    print(f"  sha256:  {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
