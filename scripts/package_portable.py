"""打包 Windows Portable ZIP + SHA256。

把 PyInstaller 的 onedir 产物（dist/SignalLens）整理成用户拿到的形态::

    SignalLens.exe
    _internal/
    README-\u542f\u52a8\u8bf4\u660e.txt
    LICENSE
    VERSION

并生成 ``SignalLens-v<version>-Windows-x64-portable.zip`` 与同名 ``.sha256``。

用法::

    python scripts/package_portable.py
    python scripts/package_portable.py --build-dir dist/SignalLens

release ZIP \u91cc\u4e0d\u5e26\u4efb\u4f55\u7528\u6237\u6570\u636e\uff08\u6ca1\u6709 cache/settings/logs\u3001\u6ca1\u6709 .env\uff09\u3002
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ("README-\u542f\u52a8\u8bf4\u660e.txt", "LICENSE", "VERSION")


def version() -> str:
    text = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    return text or "0.0.0"


def package(build_dir: Path, out_dir: Path) -> tuple[Path, Path]:
    build_dir = build_dir.resolve()
    if not (build_dir / "SignalLens.exe").exists():
        raise SystemExit(f"\u627e\u4e0d\u5230 SignalLens.exe\uff1a{build_dir}")

    name = f"SignalLens-v{version()}-Windows-x64-portable"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{name}.zip"

    with tempfile.TemporaryDirectory(prefix="SignalLens-pkg-") as tmp:
        stage = Path(tmp) / "SignalLens"
        stage.mkdir(parents=True, exist_ok=True)
        for item in build_dir.iterdir():
            target = stage / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        # \u7528\u6237\u53ef\u89c1\u6587\u4ef6\u653e\u5230 ZIP \u6839
        for name_ in ROOT_FILES:
            source = ROOT / name_
            if source.exists():
                shutil.copy2(source, stage / name_)
        # \u786e\u4fdd\u6ca1\u6709\u7528\u6237\u6570\u636e\u88ab\u6253\u8fdb\u53bb
        for banned in ("data", "cache.sqlite3", "settings.env", "runtime.json"):
            if (stage / banned).exists():
                raise SystemExit(f"release \u5305\u4e0d\u5f97\u5305\u542b\u7528\u6237\u6570\u636e\uff1a{banned}")

        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(stage.parent))

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sidecar = out_dir / f"{name}.zip.sha256"
    sidecar.write_text(f"{digest}  {name}.zip\n", encoding="utf-8")

    unpacked = sum(p.stat().st_size for p in build_dir.rglob("*") if p.is_file())
    print(f"unpacked: {unpacked / 1024 / 1024:.1f} MB")
    print(f"zip:      {zip_path.stat().st_size / 1024 / 1024:.1f} MB -> {zip_path.name}")
    print(f"exe:      {(build_dir / 'SignalLens.exe').stat().st_size / 1024 / 1024:.1f} MB")
    internal = sum(p.stat().st_size for p in (build_dir / '_internal').rglob('*')
                   if p.is_file())
    print(f"_internal:{internal / 1024 / 1024:.1f} MB")
    print(f"sha256:   {digest}")
    return zip_path, sidecar


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-dir", default=str(ROOT / "dist" / "SignalLens"))
    parser.add_argument("--out-dir", default=str(ROOT))
    args = parser.parse_args()
    package(Path(args.build_dir), Path(args.out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
