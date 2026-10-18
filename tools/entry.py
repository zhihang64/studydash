"""PyInstaller 打包入口。

包内部不能直接拿 ``src/studydash/__main__.py`` 当脚本打：那里的
``from .cli import main`` 是相对导入，被当普通脚本执行时没有包上下文，
会直接 ImportError。所以打包时用这个平铺的入口，把 ``src`` 加进
``--paths`` 后 ``import studydash`` 才是正常的包导入。

源码运行不需要这个文件（``python -m studydash`` 走 ``__main__.py``）。
"""

import sys

from studydash.cli import main

if __name__ == "__main__":
    sys.exit(main())
