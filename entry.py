"""打包成 exe 时的入口（1.9）。双击 exe（不带参数）= 打开窗口版；带参数时与 python -m guanzhe 相同。"""

import os
import subprocess
import sys

from guanzhe.__main__ import main

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):  # 控制台不是 UTF-8 时，打印中文也不出错
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if not args:
        if getattr(sys, "frozen", False) and os.name == "nt":
            # 双击启动：另起一个不带黑色窗口的进程打开窗口版，自己退出
            subprocess.Popen([sys.executable, "app"], creationflags=0x08000000 | 0x00000008)  # 无窗口 | 脱离
            sys.exit(0)
        args = ["app"]
    main(args)
