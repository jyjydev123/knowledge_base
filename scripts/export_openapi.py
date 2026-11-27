#!/usr/bin/env python3
"""把 OpenAPI schema 导出给前端用（不要求后端在跑）

为什么需要它：前端要的是**离线可用的类型**，而不是"运行中的后端返回什么我看什么"。
边跑边猜接口的代价是类型对不上却编译通过（fetch 的返回是 any 的时候尤其明显）。

    .venv/bin/python scripts/export_openapi.py            # 只 dump openapi.json
    .venv/bin/python scripts/export_openapi.py --types    # 再用 openapi-typescript 生成 schema.d.ts

生成物落在 frontend/src/api/ 下：
    openapi.json   —— 给编辑器/查接口用，体积小、可读
    schema.d.ts    —— 给 openapi-fetch 用的类型（只有 --types 且装了 node 依赖时才生成）

刻意**不**做的事：不在导入期连数据库、不起服务。app.openapi() 是纯内存构造，
所以这个脚本可以在 CI 里跑，也能在还没建库的开发机上跑。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "frontend" / "src" / "api"


def dump_spec() -> dict:
    from app.main import app

    return app.openapi()


def write_json(spec: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / "openapi.json"
    target.write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    return target


def gen_types() -> bool:
    """调 openapi-typescript 生成 schema.d.ts。node/npx 不在就返回 False 并给出一行提示"""
    npx = shutil.which("npx")
    if not npx:
        print("  ! 没找到 npx，跳过 schema.d.ts（json 已经导出，类型层是手写的，不影响构建）")
        return False
    command = [npx, "--yes", "openapi-typescript@7", "src/api/openapi.json", "-o", "src/api/schema.d.ts"]
    try:
        done = subprocess.run(command, cwd=ROOT / "frontend", capture_output=True, text=True, timeout=240, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"  ! 调用 openapi-typescript 失败：{type(exc).__name__}: {exc}")
        return False
    if done.returncode != 0:
        print("  ! openapi-typescript 报错：", (done.stderr or done.stdout).strip()[:400])
        return False
    print("  ✓ frontend/src/api/schema.d.ts")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 OpenAPI 给前端")
    parser.add_argument("--types", action="store_true", help="顺带用 openapi-typescript 生成 schema.d.ts")
    parser.add_argument("--stats", action="store_true", help="只打印端点数量统计，不写文件")
    args = parser.parse_args()

    spec = dump_spec()
    paths = spec.get("paths", {})
    operations = sum(len(v) for v in paths.values())

    if args.stats:
        print(f"路径 {len(paths)} 个、操作 {operations} 个、schema {len(spec.get('components', {}).get('schemas', {}))} 个")
        return 0

    target = write_json(spec)
    print(f"  ✓ {target.relative_to(ROOT)}（{len(paths)} 条路径 / {operations} 个操作）")

    if args.types:
        gen_types()

    print()
    print("前端类型层目前在 frontend/src/api/types.ts（手写）。")
    print("接口稳定后把 client.ts 换成 openapi-fetch + 这里生成的 schema.d.ts，")
    print("这样后端一改字段，前端编译期就会红，而不是运行时读到 undefined。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
