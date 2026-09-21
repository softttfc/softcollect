# 洛雪音源检验器

面向 Windows 的洛雪音乐（LX Music）音源校验、升级与检索工具。项目使用 Python 标准库和 tkinter 实现，支持直接运行源码，也可以通过 PyInstaller 打包为单文件 `.exe`。

## 功能

- 扫描候选 GitHub 仓库并解析音源脚本元信息
- 通过搜索/点歌请求进行在线播放级可用性校验
- 对本地音源执行失效清理和在线升级，升级前自动备份
- 音源检索、名称/内容去重、批量下载
- 图形界面与命令行入口
- 支持可选 GitHub Token（仅用于当前进程，提高 API 限额，不写入配置）

## 环境要求

- Windows
- Python 3.8+
- tkinter（通常随 Windows Python 一起安装）
- 运行源码无需第三方 Python 依赖
- 打包需要 PyInstaller：`python -m pip install pyinstaller`

## 运行源码

```bat
python lx_toolkit_app.py
```

带参数时进入命令行模式，例如：

```bat
python lx_toolkit_app.py --check --json
python lx_toolkit_app.py --discover
python lx_toolkit_app.py --discover --download --dir "D:\lx-sources"
```

完整参数可执行：

```bat
python lx_toolkit_app.py --help
```

## 打包 Windows exe

项目已提供 `build_exe.py`，会先执行源码语法检查，再调用 PyInstaller 生成单文件、无控制台窗口的 Windows 可执行文件：

```bat
python build_exe.py
python build_exe.py D:\some\output
```

默认输出到当前用户桌面；指定目录时输出为：

```text
洛雪音源工具箱.exe
```

打包产物、PyInstaller 中间文件、运行时缓存和扫描报告均不纳入版本库，详见 `.gitignore`。

## 数据与隐私

运行时会在工具目录或 exe 同目录创建报告、状态、缓存、备份和下载目录。这些文件可能包含本机路径、远程接口地址或运行结果，因此默认不提交到 Git。使用 GitHub Token 时请通过界面或命令行参数临时提供，不要把 Token 写入源码、配置文件或报告。

本工具会访问第三方 GitHub 仓库和音源接口；远程内容、接口可用性及其授权状态可能随时变化。下载和使用第三方音源前，请自行确认相关许可并遵守适用法律法规。

## 许可证

本项目采用 [MIT License](LICENSE)。项目中引用的第三方音源、图标或远程内容仍归其原作者或权利人所有。
