"""整理源码和Windows交付包，清单记录每个实际文件的摘要与环境。"""

import argparse
import hashlib
import html
import importlib.metadata as metadata
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from urllib.parse import unquote, urlsplit

from finpipe import __version__

CURRENT_DOCUMENTS = (
    "README.md",
    "windows_usage.md",
    "implementation.md",
    "architecture.md",
    "data_contract.md",
    "data_dictionary.md",
    "backtest.md",
    "factors.md",
)
DELIVERY_EVIDENCE = (
    "source_probe.json",
    "configured_backtest_v016_validation.json",
    "usability_v016_validation.json",
)


def copy_documents(root: Path, destination: Path) -> None:
    """只交付使用、技术及扩展说明；开发约定和历史评审留在本地。"""
    for name in CURRENT_DOCUMENTS:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "docs" / name, target)


def relocate_document_links(root: Path, response: Path, destination: Path) -> None:
    """按交付目录改写说明链接；封存的示例结果保持原字节。"""
    source = destination / "source"
    documents = [(root / path.relative_to(source), path) for path in source.rglob("*.md")]
    documents.extend((path, destination / "题目回应" / path.name) for path in response.glob("*.md"))

    for original, target in documents:
        content = original.read_text(encoding="utf-8")

        def replace(match: re.Match) -> str:
            """仅调整本地文件路径，外链、页内锚点及查询参数保持不变。"""
            link = urlsplit(match[2])
            if link.scheme or link.netloc or not link.path:
                return match[0]
            referenced = (original.parent / unquote(link.path)).resolve()
            for origin, delivered in ((response, destination / "题目回应"), (root, source)):
                if referenced.is_relative_to(origin):
                    relocated = delivered / referenced.relative_to(origin)
                    relative = Path(os.path.relpath(relocated, target.parent)).as_posix()
                    suffix = ("?" + link.query if link.query else "") + (
                        "#" + link.fragment if link.fragment else ""
                    )
                    return match[1] + relative + suffix + match[3]
            return match[0]

        updated = re.sub(r"(\[[^\]]*\]\()([^\s)]+)(\))", replace, content)
        if updated != content:
            target.write_text(updated, encoding="utf-8", newline="\n")


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
    # 运行资源保留原字节，仅排除重复的开发说明；依赖许可证仍完整交付。
    resource_docs = root / "dist/finpipe/_internal/finpipe/resources/demo"
    shutil.copytree(
        root / "dist/finpipe",
        binary,
        ignore=lambda directory, names: (
            {"README.md"} if Path(directory) == resource_docs else set()
        ),
    )
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
    shutil.copyfile(root / "LICENSE", binary / "LICENSE")
    (binary / "scripts").mkdir()
    shutil.copyfile(root / "scripts/scheduled_job.ps1", binary / "scripts/scheduled_job.ps1")
    shutil.copytree(root / "config", binary / "config", ignore=shutil.ignore_patterns("output"))
    source = destination / "source"
    source.mkdir()
    # 开发仓库与完整交付包中的源码位置不同，均保留唯一一份题目回应。
    response = root / "题目回应"
    if not response.is_dir():
        response = root.parent / "题目回应"
    shutil.copytree(response, destination / "题目回应")
    for name in ("src", "tests", "config", "scripts"):
        shutil.copytree(
            root / name,
            source / name,
            ignore=shutil.ignore_patterns(
                "__pycache__",
                ".pytest_cache",
                "*.egg-info",
                "output",
                "verify_backtest_delivery.py",
            ),
        )
    for name in (
        "README.md",
        "LICENSE",
        "pyproject.toml",
        "requirements.txt",
        "config.py",
        "main.py",
        "run.cmd",
        "run_demo.cmd",
        "机试题目-金融数据处理与特征工程.md",
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
    # 保留测试实际读取的来源证据及题目回应引用的验收摘要，不遍历复制旧记录。
    for name in DELIVERY_EVIDENCE:
        shutil.copyfile(root / "data/evidence" / name, source / "data/evidence" / name)
    relocate_document_links(root, response, destination)
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
        "无需Python或.venv，以下操作都在本文件所在的最外层完成。\n\n"
        "1. 点击起始页的“查看财务结果”，先看已有样例。\n"
        "2. 双击同目录run_demo.cmd离线重跑；退出码0表示成功，按窗口的“报告路径”打开HTML。"
        "固定三家公司，结果保存到output/demo。\n"
        "3. 需要自选公司时，用编辑器打开同目录config.py，修改股票和财报季度，保存后双击run.cmd。"
        "该步骤需联网，结果保存到output/financial。\n\n"
        "日常编辑最外层config.py；source/config.py用于源码开发。程序需要整个windows/finpipe目录。\n\n"
        "更多操作与排错见[Windows说明](source/docs/windows_usage.md)，"
        "回测见[完整配置示例](source/docs/backtest.md)。\n\n"
        "审核材料直接放在最外层题目回应文件夹：[题目逐项回应](题目回应/README.md)、"
        "[示例结果](题目回应/示例结果/index.html)、"
        "[测试与验收](题目回应/测试与验收.md)。\n\n"
        "源码安装见[source/README.md](source/README.md)，技术说明见"
        "[文档导航](source/docs/README.md)。独立干净Windows机器尚未验收。\n\n"
        "## 附录：完整性记录\n\n"
        "SHA256.json记录文件摘要，供维护与排错使用。"
        "正常运行无需先手工校验。\n"
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
