"""整理源码和Windows交付包，清单记录每个实际文件的摘要与环境。"""

import argparse
import hashlib
import html
import importlib.metadata as metadata
import json
import platform
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from finpipe import __version__

CURRENT_DOCUMENTS = (
    "windows_usage.md",
    "implementation.md",
    "architecture.md",
    "data_contract.md",
    "data_dictionary.md",
    "backtest.md",
    "coding_standard.md",
    "acceptance.md",
    "requirements_traceability.md",
    "delivery_validation.md",
    "original_report_validation.md",
    "delivery_review_20261014.md",
    "reviews/backtest_delivery_20261014.md",
    "reviews/delivery_fixes_20261013.md",
    "reviews/delivery_round3_20261014/business_robustness.md",
    "reviews/delivery_round3_20261014/first_run.md",
    "reviews/delivery_round3_20261014/design_size.md",
)


def copy_documents(root: Path, destination: Path) -> None:
    """只交付当前说明与实际审核，历史设计和过程记录留在原工作区。"""
    for name in CURRENT_DOCUMENTS:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "docs" / name, target)


def start_page(destination: Path, snapshot_id: str, companies: int, has_backtest: bool) -> None:
    """不依赖Markdown软件或外网的首次入口，直接链接已核验真实结果。"""
    links = f'<a href="examples/financial/snapshots/{snapshot_id}/report.html">查看{companies}家公司财务结果</a>'
    if has_backtest:
        links += ' · <a href="examples/backtest_real/report.html">查看真实近似回测</a>'
    query = ".\\windows\\finpipe\\finpipe.exe query --workspace .\\examples\\financial --company 600519.SH --period 2025Q4"
    replay = ".\\windows\\finpipe\\finpipe.exe backtest --signals .\\examples\\backtest_real\\signals.json --market .\\examples\\backtest_real\\market --time-policy publication_proxy --horizon 20 --start 2023-01-01 --end 2025-12-31 --output .\\output\\真实回测_第二次"
    body = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>金融数据项目 · 从这里开始</title>'
        "<style>body{max-width:960px;margin:36px auto;padding:0 20px;font:16px/1.8 sans-serif;color:#1e293b}"
        "a{color:#0369a1}pre{background:#f1f5f9;padding:16px;white-space:pre-wrap;word-break:break-all}"
        ".note{background:#fff7ed;padding:16px}</style>"
        f"<h1>金融数据处理与特征工程 {html.escape(__version__)}</h1><p>{links}</p>"
        "<h2>先看结果，再运行</h2><ol><li>点击上方链接看随包真实结果，无需Python和网络。</li>"
        "<li>进入windows/finpipe，双击run_demo.cmd。窗口显示结果目录和实际HTML路径。</li>"
        "<li>再次演示会复用结果；源码、测试和当前技术文档集中在source。</li></ol>"
        "<h2>在交付包根目录打开PowerShell</h2><p>查询一家公司一个报告期：</p>"
        f"<pre>{html.escape(query)}</pre><p>人工五组演示：</p>"
        "<pre>.\\windows\\finpipe\\finpipe.exe backtest --demo --output .\\output\\人工回测_第一次</pre>"
        "<p>输出已存在时换新目录，不覆盖之前的结果。回测默认使用披露累计加权ROE，报告包含日期轴、净值轴、分组图例、等权参考和首期成员；可保存独立SVG与图表CSV。</p>"
        + (
            f"<p>小体量真实输入离线复算：</p><pre>{html.escape(replay)}</pre>"
            if has_backtest
            else ""
        )
        + "<h2>评审与维护</h2><p>先读source/README.md和source/docs/architecture.md。原题、要求对照、实际测试、"
        "金融与回测口径在source中，目录说明见START_HERE.md。</p>"
        '<div class="note">真实回测按公告及已知更新日期近似放出当前财报值，历史版本未核验；'
        "后复权价格研究未单独核验分红现金流或可交易性。人工样例明确标识。独立干净Windows VM尚未验收。</div></html>"
    )
    (destination / "START_HERE.html").write_text(body, encoding="utf-8")


def digest(path: Path) -> str:
    """逐块摘要避免把完整发布包装入内存。"""
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            result.update(chunk)
    return result.hexdigest()


def package(root: Path, example_workspace: Path, backtest_example: Path | None = None):
    """新目录交付，不覆盖已经封存的发布包；原证据保持字节不变。"""
    version = __version__
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = root / "release" / ("finpipe_" + version + "_" + stamp)
    destination.mkdir(parents=True, exist_ok=False)
    binary = destination / "windows" / "finpipe"
    shutil.copytree(root / "dist/finpipe", binary)
    shutil.copyfile(root / "scripts/run_demo.cmd", binary / "run_demo.cmd")
    shutil.copyfile(root / "docs/windows_usage.md", binary / "README.md")
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(root / name, binary / name)
    for path in root.glob("*.md"):
        if not (binary / path.name).exists():
            shutil.copyfile(path, binary / path.name)
    (binary / "scripts").mkdir()
    shutil.copyfile(root / "scripts/scheduled_job.ps1", binary / "scripts/scheduled_job.ps1")
    shutil.copytree(root / "config", binary / "config", ignore=shutil.ignore_patterns("output"))
    source = destination / "source"
    source.mkdir()
    for name in ("src", "tests", "config", "scripts"):
        shutil.copytree(
            root / name,
            source / name,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "output"),
        )
    for name in (
        "README.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "AGENTS.md",
        "pyproject.toml",
        "requirements-runtime.txt",
        "requirements-dev.txt",
        "requirements-audit.txt",
        ".gitignore",
        ".gitattributes",
    ):
        shutil.copyfile(root / name, source / name)
    copy_documents(root, source / "docs")
    shutil.copytree(
        root / "data/reference/original_reports", source / "data/reference/original_reports"
    )
    shutil.copytree(
        root / "data/evidence/full_market_capture_20261013",
        source / "data/evidence/full_market_capture_20261013",
    )
    for path in sorted((root / "data/evidence").glob("*.json")):
        shutil.copyfile(path, source / "data/evidence" / path.name)
    for path in root.glob("*.md"):
        if not (source / path.name).exists():
            shutil.copyfile(path, source / path.name)
    from finpipe.ops import backup

    example = backup(example_workspace, destination / "examples")
    if example["status"] != "ok":
        raise ValueError("真实示例备份核验失败，不能生成交付包")
    original = Path(example["backup_path"]).resolve()
    financial = (destination / "examples/financial").resolve()
    if not original.is_relative_to(destination.resolve()) or not financial.is_relative_to(
        destination.resolve()
    ):
        raise ValueError("示例整理路径越过新交付目录")
    original.rename(financial)
    example_path = financial.relative_to(destination).as_posix()
    from finpipe.storage import Store

    with Store(example_workspace, readonly=True) as store:
        published = store.select_snapshot()
        example_companies = store.conn.execute(
            "SELECT COUNT(DISTINCT security_code) FROM snapshot_features WHERE snapshot_id=?",
            (published["snapshot_id"],),
        ).fetchone()[0]
    if backtest_example:
        shutil.copytree(backtest_example, destination / "examples/backtest_real")
    start_page(destination, published["snapshot_id"], example_companies, bool(backtest_example))
    backtest_start = (
        (
            "小体量真实近似回测及可独立复算输入位于 `examples/backtest_real`，"
            "报告明确历史财报版本未核验；不包含任何E盘私有行情库。\n\n"
            "```powershell\n.\\windows\\finpipe\\finpipe.exe backtest --demo --output .\\人工回测\n"
            ".\\windows\\finpipe\\finpipe.exe backtest --signals .\\examples\\backtest_real\\signals.json "
            "--market .\\examples\\backtest_real\\market --time-policy publication_proxy "
            "--start 2023-01-01 --end 2025-12-31 --horizon 20 --output .\\真实近似复算\n```\n\n"
        )
        if backtest_example
        else ""
    )
    start = (
        "# 从这里开始\n\n先双击根目录 `START_HERE.html`，无需Markdown软件即可查看真实结果和运行步骤。\n\nWindows程序在 `windows/finpipe`，双击其中的 `run_demo.cmd` 即可离线运行。"
        "源码、题目、锁定依赖和全部测试位于 `source`。\n\n"
        f"{example_companies}家公司已核验财务工作区位于 `{example_path}`，可直接查询或打开其snapshots中的HTML。\n\n"
        f"```powershell\n.\\windows\\finpipe\\finpipe.exe query --workspace .\\{example_path.replace('/', chr(92))} --company 600519.SH --period 2025Q4\n```\n\n"
        + backtest_start
        + "为控制体积，默认附小财务示例；50家公司公开原文位于source/data/evidence，重放方法见source/docs/backtest.md。\n\n"
        + "当前说明先读source/README.md、docs/architecture.md、docs/implementation.md和docs/backtest.md；审核结论见docs/delivery_review_20261014.md。历史设计、对话及旧在线capture留在原工作区；官方PDF只存于source/data/reference/original_reports。\n\n"
        "实际验收与限制见 `source/docs/delivery_validation.md`；SHA256.json及provenance.json固定文件和构建环境。\n"
    )
    (destination / "START_HERE.md").write_text(start, encoding="utf-8")
    licenses = binary / "licenses"
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        licenses.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(python_license, licenses / "CPython-LICENSE.txt")
    for distribution in metadata.distributions():
        for item in distribution.files or []:
            if any(
                "license" in part.lower() or "copying" in part.lower() or "notice" in part.lower()
                for part in item.parts
            ):
                original = distribution.locate_file(item)
                if original.is_file():
                    target = licenses / distribution.metadata["Name"] / item
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, target)
    packages = {dist.metadata["Name"]: dist.version for dist in metadata.distributions()}
    provenance = {
        "version": version,
        "created_at": stamp,
        "python": sys.version,
        "platform": platform.platform(),
        "packages": packages,
        "data_kind": "真实HTTP归档与明确人工夹具分别存放",
        "clean_machine_validation": "not_run",
        "packaging_policy": "当前源码与测试、当前文档、完整公开财务原文和小样例；历史档案留原工作区，PDF只存一份",
    }
    (destination / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    files = [
        {
            "path": str(path.relative_to(destination)).replace("\\", "/"),
            "bytes": path.stat().st_size,
            "sha256": digest(path),
        }
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    ]
    (destination / "SHA256.json").write_text(
        json.dumps(files, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    archive = Path(str(destination) + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in destination.rglob("*"):
            if path.is_file():
                bundle.write(path, path.relative_to(destination))
    print(
        json.dumps(
            {
                "directory": str(destination),
                "archive": str(archive),
                "sha256": digest(archive),
                "bytes": archive.stat().st_size,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--example-workspace", type=Path, required=True)
    arguments.add_argument("--backtest-example", type=Path)
    values = arguments.parse_args()
    package(
        Path(__file__).resolve().parents[1],
        values.example_workspace.resolve(),
        values.backtest_example.resolve() if values.backtest_example else None,
    )
