# RealtimeDialog-doubao

<div align="center">

![Python](https://img.shields.io/badge/Python-3.7%2B-3776AB?logo=python&logoColor=white)
![WebSocket](https://img.shields.io/badge/WebSocket-realtime-2ea44f)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

**一个基于火山引擎豆包实时语音对话接口的 Python 语音交互示例项目。**

通过麦克风进行实时语音对话，或用本地 WAV 文件完成单轮语音问答。

</div>

## 目录

- [项目简介](#项目简介)
- [功能特性](#功能特性)
- [项目结构](#项目结构)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [使用方式](#使用方式)
- [音频格式](#音频格式)
- [常见问题](#常见问题)
- [开发建议](#开发建议)
- [贡献指南](#贡献指南)
- [许可证](#许可证)

## 项目简介

RealtimeDialog-doubao 是一个面向开发者的语音对话示例项目，用于演示如何通过 WebSocket 调用豆包实时语音对话能力。

项目目前提供两条主流程：

- **实时语音对话**：打开麦克风，将用户语音实时发送到服务端，并播放 AI 返回的语音。
- **文件语音问答**：读取本地 WAV 音频文件，将音频发送到服务端，并把 AI 回复保存为 WAV 文件。

> 当前代码使用 `wss://openspeech.bytedance.com/api/v3/realtime/dialogue` 接口和 `X-Api-*` 请求头认证方式。请确保你的火山引擎资源、密钥和接口权限与该调用方式匹配。

## 功能特性

- 实时麦克风录音和语音回复播放
- 基于 WebSocket 的流式音频传输
- 本地 WAV 文件输入和 AI 回复音频保存
- 输入音频格式转换工具
- 本地录音工具
- 可配置 AI 角色、说话风格和音频参数
- 支持通过环境变量管理 API 密钥

## 项目结构

```text
RealtimeDialog-doubao/
├── main.py                    # 实时语音对话入口
├── audio_manager.py           # 麦克风、扬声器和实时会话管理
├── file_dialogue.py            # 文件语音问答入口
├── audio_converter.py         # WAV 转换和录音工具
├── realtime_dialog_client.py  # WebSocket 客户端
├── protocol.py                # 实时对话协议编解码
├── config.py                  # 接口、鉴权和音频参数配置
├── requirements.txt           # Python 依赖
├── LICENSE                    # 开源许可证
└── README.md                  # 项目文档
```

仓库中还包含若干日志分析脚本，例如 `log_parser.py`、`log_analyzer.py`、`log_cleaner.py` 和 `log_main.py`。这些脚本用于调试日志，不是主调用入口。

## 环境要求

- Python 3.7+
- 可访问火山引擎语音服务的网络环境
- 火山引擎侧已开通对应实时语音对话能力
- 实时语音对话需要麦克风和扬声器
- 录音、播放相关功能依赖 `pyaudio`

Windows 用户如果直接安装 `pyaudio` 失败，建议先安装对应 Python 版本的 wheel，或使用 Conda 环境安装。

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/SUAT-AIRI/RealtimeDialog-doubao.git
cd RealtimeDialog-doubao
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

如果当前系统的 `python` 或 `pip` 命令不可用，可以使用 Windows Python Launcher：

```bash
py -m pip install -r requirements.txt
```

### 3. 配置密钥

推荐使用环境变量，避免把密钥写入代码。

PowerShell：

```powershell
$env:DOUBAO_APP_ID="your-app-id"
$env:DOUBAO_ACCESS_KEY="your-access-key"
```

macOS / Linux：

```bash
export DOUBAO_APP_ID="your-app-id"
export DOUBAO_ACCESS_KEY="your-access-key"
```

### 4. 运行实时语音对话

```bash
py main.py
```

或：

```bash
python main.py
```

程序启动后会打开麦克风。直接说话即可开始对话，按 `Ctrl+C` 结束。

## 配置说明

主要配置集中在 `config.py`。

### 接口认证

```python
ws_connect_config = {
    "base_url": "wss://openspeech.bytedance.com/api/v3/realtime/dialogue",
    "headers": {
        "X-Api-App-ID": os.getenv("DOUBAO_APP_ID", ""),
        "X-Api-Access-Key": os.getenv("DOUBAO_ACCESS_KEY", ""),
        "X-Api-Resource-Id": "volc.speech.dialog",
        "X-Api-App-Key": "PlgvMymc7f3tQnJ6",
        "X-Api-Connect-Id": str(uuid.uuid4()),
    },
}
```

如果你的控制台或接口文档给出的 `Resource-Id`、`App-Key` 不同，请以实际开通资源为准。

### AI 角色

```python
start_session_req = {
    "dialog": {
        "bot_name": "豆包",
        "system_role": "你使用活泼灵动的女声，性格开朗，热爱生活。",
        "speaking_style": "你的说话风格简洁明了，语速适中，语调自然。",
    }
}
```

可以根据业务场景调整 `bot_name`、`system_role` 和 `speaking_style`。

### 音频参数

输入音频配置：

```python
input_audio_config = {
    "chunk": 3200,
    "format": "pcm",
    "channels": 1,
    "sample_rate": 16000,
    "bit_size": pyaudio.paInt16,
}
```

输出音频配置：

```python
output_audio_config = {
    "chunk": 3200,
    "format": "pcm",
    "channels": 1,
    "sample_rate": 24000,
    "bit_size": pyaudio.paFloat32,
}
```

## 使用方式

### 实时语音对话

入口文件：`main.py`

```bash
py main.py
```

工作流程：

```text
麦克风录音 -> WebSocket 发送音频块 -> 豆包实时对话服务 -> 接收语音回复 -> 扬声器播放
```

适用场景：

- 语音助手 Demo
- 实时语音交互测试
- 智能客服原型验证

### 文件语音问答

入口文件：`file_dialogue.py`

```bash
py file_dialogue.py input.wav -o output_reply.wav
```

示例：

```bash
py file_dialogue.py question.wav -o answer.wav
```

工作流程：

```text
读取 WAV 文件 -> 按音频块发送 -> 豆包实时对话服务 -> 收集回复音频 -> 保存为 WAV 文件
```

适用场景：

- 单轮语音问答
- 离线音频测试
- 接口联调

### 录制测试音频

录制 5 秒音频：

```bash
py audio_converter.py record -o question.wav -d 5
```

录制 10 秒音频：

```bash
py audio_converter.py record -o question.wav -d 10
```

### 转换 WAV 格式

将 WAV 转换为项目要求的 16kHz、16bit、单声道格式：

```bash
py audio_converter.py convert input.wav -o input_16khz.wav
```

转换后再调用文件问答：

```bash
py file_dialogue.py input_16khz.wav -o answer.wav
```

## 音频格式

文件语音问答的输入文件必须满足：

| 参数 | 要求 |
| --- | --- |
| 容器格式 | WAV |
| 采样率 | 16000 Hz |
| 位深 | 16 bit |
| 声道 | 单声道 |

当前 `audio_converter.py` 使用 Python 标准库 `wave` 读取音频，因此只支持 WAV 输入。MP3、M4A、AAC 等格式需要先用其他工具转换为 WAV，或在项目中额外引入 `ffmpeg`、`pydub` 等音频处理依赖。

## 常见问题

### 连接失败怎么办？

请检查：

- `DOUBAO_APP_ID` 和 `DOUBAO_ACCESS_KEY` 是否已配置。
- `config.py` 中的 `base_url`、`X-Api-Resource-Id`、`X-Api-App-Key` 是否与实际资源一致。
- 当前网络是否可以访问火山引擎语音服务。
- 火山引擎控制台中是否已开通对应能力。

### 为什么实时模式没有声音？

请检查：

- 系统麦克风权限是否已开启。
- 扬声器或耳机是否可用。
- `pyaudio` 是否正常安装。
- 当前环境是否能打开输入流和输出流。

### 文件问答提示音频格式错误怎么办？

先转换音频：

```bash
py audio_converter.py convert input.wav -o input_16khz.wav
py file_dialogue.py input_16khz.wav -o answer.wav
```

### 支持批量处理吗？

当前仓库没有独立的 `batch_dialog.py`。如果需要批量处理，可以基于 `file_dialogue.py` 的命令行入口循环处理多个 WAV 文件。

### 支持 MP3 吗？

当前不直接支持 MP3。请先转换为 WAV，再使用本项目处理。

## 开发建议

- 不要把 API 密钥写入提交记录，优先使用环境变量。
- 不要提交运行产物，例如 `output.log`、`parsed_log.json`、`output.wav`、`response_audio_*.wav`。
- 如果要对外发布，建议补充测试、示例音频、依赖版本范围和 CI 检查。

## 路线图

- [ ] 增加批量处理脚本
- [ ] 支持更多音频格式输入
- [ ] 增加 `.env` 配置示例
- [ ] 补充最小化自动化测试
- [ ] 优化错误提示和日志输出

## 贡献指南

欢迎提交 Issue 和 Pull Request。

建议流程：

1. Fork 本仓库
2. 创建功能分支：`git checkout -b feature/your-feature`
3. 提交修改：`git commit -m "Add your feature"`
4. 推送分支：`git push origin feature/your-feature`
5. 创建 Pull Request

提交代码前，请确认：

- README 中的命令和实际文件名一致。
- 不包含 API 密钥、日志文件或音频运行产物。
- Python 文件可以通过基础编译检查：

```bash
py -m compileall -q .
```

## 许可证

本项目基于 MIT License 开源，详见 [LICENSE](LICENSE)。
