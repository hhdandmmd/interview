"""整理源码和Windows交付包，清单记录每个实际文件的摘要与环境。"""

import argparse
import hashlib
import html
import importlib.metadata as metadata
import json
import platform
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from finpipe import __version__

CURRENT_DOCUMENTS = (
    "README.md",
    "source_capture_implementation.md",
    "windows_usage.md",
    "implementation.md",
    "architecture.md",
    "data_contract.md",
    "data_dictionary.md",
    "backtest.md",
    "factors.md",
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
    """模板只填写当前版本和实际样例链接，首次操作说明保持可单独阅读。"""
    template = Template(Path(__file__).with_name("start_here.html").read_text(encoding="utf-8"))
    page = template.substitute(
        version=html.escape(__version__),
        companies=companies,
        financial_report=f"examples/financial/snapshots/{html.escape(snapshot_id)}/report.html",
        backtest_link=(
            '<a class="button" href="examples/backtest_real/report.html">查看ROE五组回测</a>'
            if has_backtest
            else ""
        ),
    )
    (destination / "START_HERE.html").write_text(page, encoding="utf-8", newline="\n")


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
    for name in ("config.py", "main.py", "run.cmd", "run_demo.cmd"):
        shutil.copyfile(root / name, destination / name)
    shutil.copyfile(root / "scripts/run_demo.cmd", binary / "run_demo.cmd")
    # 程序子目录只指回交付根目录，避免把相对链接和命令复制到错误位置。
    (binary / "README.md").write_text(
        "# Windows程序目录\n\n请回到交付包最外层，打开[START_HERE.html](../../START_HERE.html)。\n\n"
        "离线演示双击最外层run_demo.cmd，自选公司编辑最外层config.py后双击run.cmd。\n\n"
        "操作与排错见[Windows说明](../../source/docs/windows_usage.md)。"
        "本目录及依赖须整体保留；这里的旧run_demo.cmd兼容入口将结果写到用户目录。\n",
        encoding="utf-8",
        newline="\n",
    )
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(root / name, binary / name)
    (binary / "scripts").mkdir()
    shutil.copyfile(root / "scripts/scheduled_job.ps1", binary / "scripts/scheduled_job.ps1")
    shutil.copytree(root / "config", binary / "config", ignore=shutil.ignore_patterns("output"))
    source = destination / "source"
    source.mkdir()
    shutil.copytree(root / "题目回应", source / "题目回应")
    for name in ("src", "tests", "config", "scripts"):
        shutil.copytree(
            root / name,
            source / name,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.egg-info", "output"),
        )
    for name in (
        "README.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "AGENTS.md",
        "pyproject.toml",
        "requirements.txt",
        ".gitignore",
        ".gitattributes",
        "config.py",
        "main.py",
        "run.cmd",
        "run_demo.cmd",
    ):
        shutil.copyfile(root / name, source / name)
    copy_documents(root, source / "docs")
    shutil.copytree(
        root / "data/reference/original_reports", source / "data/reference/original_reports"
    )
    shutil.copytree(root / "data/examples", source / "data/examples")
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
    from finpipe.storage import Store

    with Store(example_workspace, readonly=True) as store:
        published = store.select_snapshot()
        example_companies = store.conn.execute(
            "SELECT COUNT(DISTINCT security_code) FROM snapshot_features WHERE snapshot_id=?",
            (published["snapshot_id"],),
        ).fetchone()[0]
    if backtest_example:
        from finpipe.backtest.io import load_market, load_signals, run_backtest

        signals, signal_metadata = load_signals(
            backtest_example / "signals.json", "publication_proxy"
        )
        run_backtest(
            signals,
            signal_metadata,
            load_market(backtest_example / "market"),
            feature="roe_weighted_ytd",
            output=destination / "examples/backtest_real",
            start="2023-01-01",
            end="2025-12-31",
            horizon=20,
        )
    start_page(destination, published["snapshot_id"], example_companies, bool(backtest_example))
    start = (
        "# 从这里开始\n\n先解压整个ZIP，再双击同目录的[START_HERE.html](START_HERE.html)。"
        "无需Python，以下操作都在本文件所在的最外层完成。\n\n"
        "1. 点击起始页的“查看财务结果”，先看已有样例。\n"
        "2. 双击同目录run_demo.cmd离线重跑；退出码0表示成功，按窗口的“报告路径”打开HTML。"
        "固定三家公司，结果保存到output/demo。\n"
        "3. 需要自选公司时，用编辑器打开同目录config.py，修改股票和财报季度，保存后双击run.cmd。"
        "该步骤需联网，结果保存到output/financial。\n\n"
        "日常编辑最外层config.py；source/config.py用于源码开发。程序需要整个windows/finpipe目录。\n\n"
        "更多操作与排错见[Windows说明](source/docs/windows_usage.md)，"
        "回测见[完整配置示例](source/docs/backtest.md)。\n\n"
        "审核材料：[题目逐项回应](source/题目回应/README.md)、"
        "[示例结果](source/题目回应/示例结果/index.html)、"
        "[测试与验收](source/题目回应/测试与验收.md)。\n\n"
        "源码安装见[source/README.md](source/README.md)，技术说明见"
        "[文档导航](source/docs/README.md)。SHA256.json记录交付文件摘要，"
        "provenance.json记录构建环境。独立干净Windows机器尚未验收。\n"
    )
    (destination / "START_HERE.md").write_text(start, encoding="utf-8", newline="\n")
    licenses = binary / "licenses"
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        licenses.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(python_license, licenses / "CPython-LICENSE.txt")
    for distribution in metadata.distributions():
        for item in distribution.files or []:
            # 按许可证文件名收集，避免将licenses模块的实现和字节码缓存作为许可材料。
            if item.name.lower().startswith(("license", "licence", "copying", "notice")):
                original = distribution.locate_file(item)
                if original.is_file():
                    target = licenses / distribution.metadata["Name"] / item
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, target)
    # 可编辑安装可能遗留重复元数据，采用与metadata.version一致的首个有效记录。
    packages = {}
    for dist in metadata.distributions():
        packages.setdefault(dist.metadata["Name"], dist.version)
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
    root = Path(__file__).resolve().parents[1]
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("--example-workspace", type=Path, help="省略时离线生成小财务样例")
    arguments.add_argument(
        "--backtest-example", type=Path, default=root / "data/examples/backtest_real"
    )
    values = arguments.parse_args()
    with tempfile.TemporaryDirectory(prefix="finpipe_package_") as temporary:
        workspace = values.example_workspace
        if workspace is None:
            from finpipe.config import load_config
            from finpipe.pipeline import execute_manifest

            # 从固定真实归档重放，保留原观察时间，不依赖历史运行目录或网络。
            workspace = Path(temporary) / "financial"
            result = execute_manifest(
                root / "src/finpipe/resources/demo/manifest.json",
                load_config(root / "config/demo.toml", workspace),
            )
            if result["status"] != "PUBLISHED":
                raise ValueError(f"离线财务样例未成功发布：{result}")
        package(root, workspace.resolve(), values.backtest_example.resolve())
