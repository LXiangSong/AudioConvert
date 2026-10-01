# AudioConvert

AudioConvert 是一个适用于 Windows 10/11 64 位的本地音频转换工具。程序使用原生窗口界面，不需要浏览器、Python 或单独安装 FFmpeg。

## 下载

打开本项目的 [Releases](https://github.com/LXiangSong/AudioConvert/releases) 页面，下载 `AudioConvert.exe`，双击即可运行。

当前版本：**v0.1.0**

## 使用方法

1. 点击“添加文件”选择音频，也可以选择原音频文件夹。
2. 选择输出文件夹，默认输出为 MP3。
3. 默认保留源文件。取消“保留源文件”后，只有转换成功并完成完整校验的源文件才会移入回收站。
4. 点击“开始转换”。转换过程中可以取消，失败任务可以重试，完成任务可以重新转换。
5. 双击任务或右键选择“查看详情”，可以查看转换结果和失败原因。

窗口会跟随 Windows 浅色或深色主题。支持同时处理 1–4 个任务，默认同时处理 2 个。

## 支持格式

输出格式：MP3、M4A、FLAC、WAV、OGG、Opus、AAC。

输入格式：MP3、M4A、AAC、WAV、FLAC、OGG、Opus、WMA、APE、AIFF、ALAC、XM，以及酷狗 KGG v5。

### 酷狗 KGG

KGG 文件需要对应歌曲的酷狗密钥。程序会自动查找：

- `%APPDATA%\\KuGou8`
- `%APPDATA%\\KuGou`
- `%LOCALAPPDATA%\\KuGou`
- 音频文件所在目录
- 程序所在目录及 `keys` 子目录

也可以在“高级设置”中手动选择 `KGMusicV3.db` 或导出的 `kgg.key`。数据库必须来自下载这些歌曲的电脑；复制前请完全退出酷狗，确保数据库写入完成。

失败时会区分提示：未检测到数据库、数据库损坏、没有读取权限、数据库没有这首歌的密钥、密钥无效，以及 KGG 版本不支持。密钥库只在本机内存中读取，不上传，也不会导出明文密钥。

## 文件保护

- 不覆盖已有输出文件，重名文件自动添加编号。
- 先写入临时文件，完成完整解码校验后再生成正式输出。
- 转换失败或取消时保留源文件。
- 取消“保留源文件”后只使用 Windows 回收站，不执行永久删除。
- 回收前会重新检查源文件大小、时间和 SHA-256，防止误处理被修改的文件。
- 输出文件夹中的音频不会被再次扫描。

## 从源码运行

需要 Python 3.13 64 位。安装依赖后运行：

```powershell
.venv\\Scripts\\python.exe -m pip install -r requirements.txt
.venv\\Scripts\\python.exe main.py
```

运行常规转换测试：

```powershell
.venv\\Scripts\\python.exe verify.py
.venv\\Scripts\\python.exe verify_kgg.py
```

## 打包 EXE

关闭正在运行的 AudioConvert 后，双击 `build.bat`。脚本会自动准备虚拟环境、安装依赖、打包 EXE，并检查程序是否能正常启动。

成功生成的文件位于：

```text
dist\\AudioConvert.exe
```

打包日志位于 `output\\build.log`，版本信息和 SHA-256 位于 `output\\build-report.json`。

## 项目文件

| 文件 | 用途 |
| --- | --- |
| `main.py` | 原生界面、队列、扫描和后台任务 |
| `engine.py` | FFmpeg 转换、校验、源文件保护 |
| `kgg_core.py` | KGG v5、QMC2 和酷狗密钥库读取 |
| `xm_core.py` | XM 文件还原 |
| `build.bat`、`build.py` | Windows 打包脚本 |
| `verify.py` | 常规音频格式回归测试 |
| `verify_kgg.py` | KGG 解码和转换测试 |
| `THIRD_PARTY.md` | 第三方组件和许可证说明 |

## 许可证说明

项目中的第三方组件分别受其原许可证约束，详情见 [THIRD_PARTY.md](THIRD_PARTY.md)。发布或二次分发时请保留相应的许可证和来源说明。
