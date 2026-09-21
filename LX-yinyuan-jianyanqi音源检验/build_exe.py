# -*- coding: utf-8 -*-
"""洛雪音源工具箱 —— 一键构建脚本（Python 标准库 + PyInstaller）

流程：
  1) python -m py_compile 校验源码文件语法
  2) PyInstaller --onefile --noconsole 打包为单文件 exe，输出到桌面

说明：已移除内置音源清单体系（sources.json / embedded_manifest.py），
      不再向 exe 内嵌任何默认清单，校验页全量在线扫描候选仓库。

用法：
  python build_exe.py                 # 打包到当前用户桌面
  python build_exe.py D:\\some\\dir    # 打包到指定目录
"""

import os
import pathlib
import py_compile
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ENTRY = "lx_toolkit_app.py"
EXE_NAME = "洛雪音源工具箱"
SOURCES = ["lx_source_updater.py", ENTRY]
# 洛雪（LX Music）官方图标，同时用于 exe 文件图标与 GUI 窗口/任务栏图标
ICON_NAME = "app_icon.ico"


def log(message):
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("utf-8", "replace").decode("utf-8", "replace"))


def output_dir():
    if len(sys.argv) > 1:
        return pathlib.Path(sys.argv[1]).expanduser().resolve()
    return pathlib.Path(os.path.expanduser("~")) / "Desktop"


def compile_check():
    for name in SOURCES:
        py_compile.compile(str(HERE / name), doraise=True)
        log("      py_compile OK: %s" % name)
    log("[1/2] 语法校验通过（python -m py_compile）")


def build(target):
    target.mkdir(parents=True, exist_ok=True)
    work = HERE / "pyi-build"
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--noconsole",
        "--name", EXE_NAME,
        "--distpath", str(target),
        "--workpath", str(work),
        "--specpath", str(work),
    ]
    icon = HERE / ICON_NAME
    if icon.is_file():
        # exe 文件图标（资源管理器 / 桌面显示）
        command += ["--icon", str(icon)]
        # 同时把 ico 打进包内，供 GUI 设置窗口与任务栏图标使用
        command += ["--add-data", "%s%s." % (icon, os.pathsep)]
        log("      exe 图标 + 窗口图标：%s" % icon.name)
    else:
        log("      未找到 %s，本次打包不带自定义图标" % ICON_NAME)
    command.append(str(HERE / ENTRY))
    log("[2/2] 开始打包：%s" % " ".join(command[1:4]))
    result = subprocess.run(command, cwd=str(HERE))
    if result.returncode != 0:
        log("打包失败，PyInstaller 返回码 %d" % result.returncode)
        return None
    exe = target / (EXE_NAME + (".exe" if os.name == "nt" else ""))
    if not exe.exists():
        log("打包结束但未找到产物：%s" % exe)
        return None
    log("打包完成：%s（%.2f MB）" % (exe, exe.stat().st_size / 1048576.0))
    return exe


def main():
    target = output_dir()
    log("源码目录：%s" % HERE)
    log("输出目录：%s" % target)
    compile_check()
    exe = build(target)
    return 0 if exe else 1


if __name__ == "__main__":
    sys.exit(main())
