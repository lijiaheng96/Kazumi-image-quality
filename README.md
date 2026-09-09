# Kazumi 本地画质助手

独立运行的 Windows 本地工具：读取你已安装的 Kazumi 网站规则，搜索同一部番剧，对不同网站的同一集取样，估计画质并排列来源。

它不修改 Kazumi 源码、安装文件或规则文件。原项目可以继续正常拉取 main 更新。本工具自身的代码维护在这个独立仓库。

## 在这台电脑上使用

工作目录：`D:\git\Kazumi-image-quality`。

1. 双击 `start.cmd`，或使用不显示终端窗口的 `启动画质助手.vbs`。
2. 浏览器打开 `http://127.0.0.1:18765`。默认选择自动发现的非空 Kazumi 规则文件。
3. 输入番剧名称，点击“搜索来源”。确认每个网站选中的是同一部、同一季；唯一精确名称匹配会自动预选。
4. 选择至少两个网站，点击“开始画质检测”。集数留空时自动选择覆盖网站最多的最早一集，也可指定集号。
5. 查看网站及线路排名，点击“打开源站”播放。工具不能在未修改 Kazumi 的情况下为它添加内部按钮。

重复启动只会打开已有页面。关闭浏览器不会停止本地服务；需要退出时双击 `stop.cmd` 或 `停止画质助手.vbs`。停止会结束本工具的任务和子进程，不会结束 Kazumi。

## 排名的含义

- 使用真实的 MUSIQ 无参考画质模型，在本机 CPU 上推理；不是按码率、分辨率或网站名称加权。
- 每个来源尝试四个正文位置，每个位置检查三个相邻画面。先匹配内容，再比较所有入榜来源共同拥有的至少三个位置。
- 来源有多个线路时，网站名次依据其最佳已检测线路，表格保留每条线路的结果。
- 模型分数不是百分制正确率。**目前属于实验性画质估计，尚未完成动画领域人工校准。** 小幅分差不代表肉眼一定可见。
- 结果只代表所选这一集的抽样，不保证整集、整部作品或整个网站都保持同样画质；不评估音质、加载速度和全部运动伪影。
- 只有一个可比较网站、共同画面不足或内容不一致时不会生成跨站冠军；失败也不会记为零分。

## 当前兼容范围

- Kazumi XPath 和 API 规则（JSONPath、请求模板、嵌套及分隔字符串章节）。规则文件每次搜索重新只读加载。
- 使用本机 Microsoft Edge 的隔离无界面会话解析播放页和 iframe，不访问日常浏览器的登录会话。
- 支持 FFmpeg 可以读取且时长有限的公开视频流。直播、HDR 混排、画幅明显不同、无法对齐的版本不纳入比较。
- 局部时间偏移搜索窗口约 ±40 秒；更长片头差异、不同剪辑或强水印可能导致无法对齐。
- 验证码、登录、交互播放、特殊加密或失效规则可能不可用；在 Kazumi 中验证不会自动同步到助手。
- 首版不把 Cookie 或 Authorization 交给 FFmpeg。带有这些凭据的媒体会明确提示暂不支持，避免在重定向和跨域分片请求中错误转发。普通统计 Cookie 也可能触发此限制。
- 网站响应有大小和超时限制。异常慢速响应头及压缩流不保证硬实时截止；取消时可能需要等待当前网络读取结束。

## 本地数据与仓库

以下目录已加入 `.gitignore`，不会上传 Git：

- `.runtime/`：本机独立 Python 解释器。
- `.venv/`：本工具专用依赖。
- `data/`：模型权重、配置、运行日志与验证记录。
- `.idea/`：本机 IDE 配置。

临时帧在检测完成或正常取消后清理。异常终止可能留下 `data/temp/` 下的临时文件，可以在停止工具后手动清理。来源视频和规则内容不上传到评分服务，首次准备依赖与模型需要联网下载。

## 从仓库重新安装

这台电脑的运行环境由当前工作完成配置。其他路径或机器需要 Python 3.12、PowerShell 7、Microsoft Edge，以及首次安装时的网络连接：

```powershell
pwsh -File .\setup.ps1 -PythonPath 'C:\Python312\python.exe'
```

如果本目录已有 `.runtime\python.exe`，可省略 `-PythonPath`。本机已配置的解释器、依赖和模型合计约 2 GB，其中 MUSIQ 权重约 104 MB；这些文件不会提交 Git。它不是单个很小的 DLL 插件。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
node tests/test_ui_contract.js
.\.venv\Scripts\python.exe -X utf8 app.py --no-browser
```

手动公网验证脚本位于 `tests/verify_installed_sources.py` 和 `tests/verify_media_sources.py`，只在明确执行时读取本机规则并访问网站，结果写入被忽略的 `data/`。单元测试使用临时文件、本地 HTTP 服务或固定响应，不修改实际 Kazumi 数据。

## 依赖与参考

- [Kazumi](https://github.com/Predidit/Kazumi)：规则格式兼容参考，代码独立实现。
- [IQA-PyTorch](https://github.com/chaofengc/IQA-PyTorch)：MUSIQ 实现及权重。
- [MUSIQ 原始研究](https://github.com/google-research/google-research/tree/master/musiq)：多尺度无参考图像质量评估。
- [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)：本地 FFmpeg 可执行文件。

本仓库不包含第三方运行库或模型权重；各依赖及模型受各自许可约束。
