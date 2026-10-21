"""测试辅助（2.11，GitHub 上 Windows 自动测试暴露的问题）：Windows 不允许删除还开着的文件。

测试里用临时文件夹放项目，项目的账本（project.db）若还有数据库连接没关，测试结束清理临时文件夹时
Windows 会报 PermissionError（Linux、macOS 不报）。这只是测试收尾的问题：报错时测试本身的检查都已通过。
这里让清理先回收不再使用的连接再删；仍删不掉（连接还被测试里的变量拿着）就跳过，留给系统清理临时文件夹。
只在 Windows 上生效。"""
import gc
import shutil
import sys
import tempfile

if sys.platform == "win32":
    _orig_cleanup = tempfile.TemporaryDirectory.cleanup

    def _cleanup(self):
        gc.collect()
        try:
            _orig_cleanup(self)
        except OSError:
            gc.collect()
            shutil.rmtree(self.name, ignore_errors=True)

    tempfile.TemporaryDirectory.cleanup = _cleanup
