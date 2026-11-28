"""data/ 目录对账与清理：DB 里有的原件一个都不能丢，DB 里没有的一律算孤儿

    .venv/bin/python scripts/prune_data.py             # 只报告，不删（默认，安全）
    .venv/bin/python scripts/prune_data.py --apply     # 真删孤儿文件 + 收空目录
    .venv/bin/python scripts/prune_data.py --quiet     # 只打结论，不列明细

为什么需要它：pytest 的上传用例走的是**真路径规则**（settings.DATA_DIR 默认 data/），
文件是真写到工作区的，而库是跑完就丢的临时 SQLite —— 所以每跑一次测试，
data/ 下就多一堆 data/{uuid}/{uuid}/{uuid}.md 孤儿（本机一次就 44 个）。
现在 DATA_DIR 可配，但测试还没接过去，所以先靠这个脚本对账收尾。

安全边界（跟 process_document 的规矩一致）：
  1. **只删文件，绝不 rmtree**；目录只在已经空了的时候 rmdir，一层层往上收；
  2. DB 里出现过的 file_path 全部保留，**软删的文档也算数**（软删是要能恢复的）；
  3. 配置里正在用的 MEMORY_DIR 子树直接跳过 —— 那是在用的数据，不是垃圾；
  4. 连不上数据库就直接退出，不猜、不删（宁可留垃圾，不能删掉原件）；
  5. 顺带报「路径漂移」：DB 记的 file_path 和按规则复算的不一致时只报告，
     因为这时候两边都可能对，删哪份都是赌。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.logging_conf import setup_logging  # noqa: E402

# 脚本也要配日志：app.services.vector / app.services.document 那几条 INFO
# （耗时、批量进度、失败原因）在没有 handler 时是被静默丢掉的，见 docs/OPERATIONS.md
setup_logging()
from app.db import models  # noqa: F401,E402  确保文档模型注册
from app.db.models import Document, KnowledgeBase  # noqa: E402
from app.db.psql_config import AsyncSessionLocal  # noqa: E402
from app.services.process_document import data_root, document_storage_path  # noqa: E402


def _walk_files(root: Path, skip: Path) -> list[Path]:
    """列出 root 下的普通文件；不跟随符号链接，也不进 skip 子树"""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath).resolve()
        if current == skip or skip in current.parents:
            dirnames[:] = []
            continue
        for name in filenames:
            found.append(Path(dirpath) / name)
    return found


def _dirs_emptied_by(root: Path, doomed: set[Path]) -> list[Path]:
    """删掉 doomed 之后**会变成空**的目录（自底向上，含本来就空的）

    dry-run 也要能预告这件事，否则你看着 68 个孤儿文件却不知道 --apply 之后会剩下一堆空壳目录。
    """
    doomed = {path.resolve() for path in doomed}
    emptied: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        if current == root:
            continue
        if any(Path(dirpath, name).resolve() not in doomed for name in filenames):
            continue
        emptied.append(current)
    return emptied


def _prune_empty_dirs(root: Path, apply: bool) -> int:
    """自底向上收掉空目录（只 rmdir，非空/失败就留着）"""
    removed = 0
    for dirpath, dirnames, _files in sorted(os.walk(root, followlinks=False), key=lambda x: -len(x[0])):
        current = Path(dirpath)
        if current == root or dirnames:
            continue
        try:
            if apply:
                current.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


async def _live_paths() -> tuple[set[Path], list[tuple[str, str]]]:
    """返回 (DB 认可的绝对路径集合, 路径漂移列表)"""
    live: set[Path] = set()
    drift: list[tuple[str, str]] = []
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(
                    Document.id, Document.file_path, Document.owner_id,
                    Document.kb_id, KnowledgeBase.owner_id,
                )
                .select_from(Document)
                .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
            )
        ).all()
    for doc_id, file_path, uploader_id, kb_id, kb_owner_id in rows:
        text = str(file_path or "").strip()
        if not text:
            continue
        live.add(Path(text).resolve())
        ext = Path(text).suffix.lstrip(".")
        try:
            expected = document_storage_path(kb_owner_id, uploader_id, kb_id, doc_id, ext)
        except ValueError:
            expected = ""  # 历史脏数据里的 id/扩展名不合规则，只按 file_path 认
        if expected and Path(expected).resolve() != Path(text).resolve():
            drift.append((text, expected))
    return live, drift


def main() -> int:
    parser = argparse.ArgumentParser(description="data/ 目录与数据库对账、清理孤儿文件")
    parser.add_argument("--apply", action="store_true", help="真的删（默认只报告）")
    parser.add_argument("--quiet", action="store_true", help="不列明细，只给结论")
    args = parser.parse_args()

    root = Path(data_root()).resolve()
    if not root.is_dir():
        print(f"{root} 不存在，没什么可对账的")
        return 0

    try:
        live, drift = asyncio.run(_live_paths())
    except Exception as exc:  # noqa: BLE001  连不上库就不许往下删
        print(f"x 连不上数据库（{type(exc).__name__}: {str(exc)[:160]}）", file=sys.stderr)
        print("  对账必须以 DB 为准，连不上就什么都不会删。先把 PG 起来再跑。", file=sys.stderr)
        return 1

    memory_dir = Path(str(settings.MEMORY_DIR)).resolve()
    files = _walk_files(root, memory_dir)
    orphans = [f for f in files if f not in live]
    gone = sorted(p for p in live if not p.is_file())

    print(f"根目录     {root}")
    print(f"DB 记录    {len(live)} 个 file_path（含软删的文档）")
    print(f"磁盘文件   {len(files)} 个" + (f"（已跳过记忆车道在用的 {memory_dir}）" if memory_dir.is_dir() else ""))
    print(f"孤儿文件   {len(orphans)} 个   ← DB 里没有对应记录")
    print(f"缺失文件   {len(gone)} 个   ← DB 说有、磁盘上没有（记录得留着，/status 会报）")
    print(f"路径漂移   {len(drift)} 个   ← DB 记的路径和规则复算的不一致")

    if not args.quiet:
        for path in orphans[:40]:
            print(f"  孤儿  {path.relative_to(root)}")
        if len(orphans) > 40:
            print(f"  ... 还有 {len(orphans) - 40} 个")
        for path in gone[:20]:
            print(f"  缺失  {path.relative_to(root) if root in path.parents else path}")
        for stored, expected in drift[:20]:
            print(f"  漂移  记录={stored}  复算={expected}")

    empty_after = _dirs_emptied_by(root, {path.resolve() for path in orphans})
    print(f"删完会变空  {len(empty_after)} 个目录  ← --apply 时会自底向上 rmdir（非空绝不强删）")
    if not args.quiet:
        for path in empty_after[:10]:
            print(f"  空壳  {path.relative_to(root)}")

    deleted = 0
    if args.apply:
        for path in orphans:
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                print(f"  ! 删不掉 {path}: {exc}", file=sys.stderr)
    if args.apply:
        dirs_removed = _prune_empty_dirs(root, apply=True)
        print(f"已删孤儿文件 {deleted} 个，收掉空目录 {dirs_removed} 个")
    else:
        print(f"dry-run：可删 {len(orphans)} 个孤儿文件、可收 {len(empty_after)} 个空目录（= 上面那个数）。"
              f"确认无误后加 --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
