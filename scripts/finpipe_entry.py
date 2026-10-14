"""供PyInstaller构建使用，运行入口与源码命令保持一致。"""

from finpipe.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
