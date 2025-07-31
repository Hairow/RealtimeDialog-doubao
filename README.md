# 豆包语音对话系统 (RealtimeDialog-doubao)

<div align="center">

[![Python Version](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![WebSocket](https://img.shields.io/badge/websocket-supported-brightgreen.svg)](https://websockets.readthedocs.io/)

基于字节跳动豆包AI的语音对话系统，提供实时对话和非实时问答两种模式

</div>

## 📖 项目介绍

本项目提供了两种与豆包AI进行语音交互的方式：

### 🎙️ 路线一：实时在线语音对话
- **入口程序**：`main.py`
- **功能特点**：通过麦克风实时与AI对话，支持连续交互和打断
- **适用场景**：需要即时互动的对话场景，如语音助手、智能客服

### 📁 路线二：非实时单轮问答
- **入口程序**：`file_based_dialog.py`
- **功能特点**：处理预录制音频文件，获取AI语音回复
- **适用场景**：批量处理、离线问答、音频文件分析

## 🚀 快速开始

### 环境要求
- Python 3.7+
- 麦克风设备（仅实时对话模式需要）
- 支持的操作系统：Windows、macOS、Linux

### 安装步骤

1. **克隆项目**
```bash
git clone https://github.com/SUAT-AIRI/RealtimeDialog-doubao.git
cd RealtimeDialog-doubao
```

2. **安装依赖**
```bash
pip install -r requirements.txt
```

3. **配置API密钥**

编辑 `config.py`，填入您的豆包API认证信息：
```python
ws_connect_config = {
    "headers": {
        "X-Api-App-ID": "your-app-id",        # 替换为您的App ID
        "X-Api-Access-Key": "your-access-key", # 替换为您的Access Key
    }
}
```

> 💡 获取API密钥：访问 [火山引擎控制台](https://console.volcengine.com/ark/region:ark+cn-beijing/tts/realTimeRecognition?)

## 📚 使用指南

### 路线一：实时在线语音对话

#### 基本使用

```bash
python main.py
```

运行后：
- ✅ 程序会自动打开麦克风
- 🎤 直接对着麦克风说话，AI会实时回复
- 🔄 支持连续对话，可以随时打断
- ⏹️ 按 `Ctrl+C` 结束对话

#### 工作原理
```
用户语音 → 麦克风录音 → WebSocket实时传输 → 豆包AI处理 → 语音回复 → 扬声器播放
```

#### 特点说明
- **低延迟**：实时传输，快速响应
- **自然交互**：支持打断和连续对话
- **流式处理**：边说边传，边收边播

### 路线二：非实时单轮问答

#### 基本使用

```bash
# 处理默认文件 input.wav
python file_based_dialog.py
```

#### 自定义文件处理

修改 `file_based_dialog.py` 中的文件路径：
```python
if __name__ == "__main__":
    input_file = "your_question.wav"   # 您的问题音频
    output_file = "ai_answer.wav"      # AI回复保存位置
```

#### 工作原理
```
音频文件 → 读取完整音频 → 一次性发送 → 豆包AI处理 → 接收完整回复 → 保存音频文件
```

#### 批量处理

使用批量处理工具处理多个文件：

```bash
# 处理多个文件
python batch_dialog.py file1.wav file2.wav file3.wav

# 处理整个目录
python batch_dialog.py audio_directory/

# 指定输出目录
python batch_dialog.py input/ -o output/
```

批量处理结果会生成详细的JSON报告。

## 🛠️ 辅助工具

### 音频录制工具

录制问题音频用于非实时问答：

```bash
# 录制5秒音频
python audio_converter.py record

# 录制自定义时长
python audio_converter.py record -o question.wav -d 10
```

### 音频格式转换

将音频转换为系统要求的格式（16kHz, 16-bit, 单声道）：

```bash
# 自动转换
python audio_converter.py convert input.mp3

# 指定输出
python audio_converter.py convert input.mp3 -o output.wav
```

## 📊 两种模式对比

| 特性 | 实时对话 (main.py) | 非实时问答 (file_based_dialog.py) |
|------|-------------------|----------------------------------|
| 交互方式 | 麦克风实时输入 | 音频文件输入 |
| 响应速度 | 毫秒级延迟 | 秒级延迟 |
| 对话模式 | 连续多轮对话 | 单轮问答 |
| 支持打断 | ✅ 支持 | ❌ 不支持 |
| 批量处理 | ❌ 不支持 | ✅ 支持 |
| 离线处理 | ❌ 需要实时连接 | ✅ 可预录制 |
| 适用场景 | 交互式应用 | 批量处理、自动化 |

## 📁 项目结构

```
doubao-voice-dialog/
├── main.py                    # [路线1] 实时对话主程序
├── audio_manager.py           # [路线1] 音频设备管理器
│
├── file_based_dialog.py       # [路线2] 非实时问答主程序
├── batch_dialog.py            # [路线2] 批量处理工具
│
├── audio_converter.py         # [工具] 音频格式转换和录制
├── realtime_dialog_client.py  # [核心] WebSocket客户端
├── protocol.py                # [核心] 通信协议
├── config.py                  # [配置] 系统配置
│
├── requirements.txt           # 依赖列表
└── README.md                  # 项目文档
```

## ⚙️ 高级配置

### 自定义AI角色

在 `config.py` 中修改AI的性格和说话风格：

```python
start_session_req = {
    "dialog": {
        "bot_name": "豆包",
        "system_role": "你是一个专业的客服助手...",  # 自定义角色
        "speaking_style": "你说话温柔耐心...",       # 自定义风格
    }
}
```

### 音频参数调整

```python
# 实时对话音频配置
input_audio_config = {
    "chunk": 3200,           # 缓冲区大小（影响延迟）
    "sample_rate": 16000,    # 采样率
}

# 非实时问答可能需要更大的缓冲区
```

## 🔍 使用示例

### 示例1：构建语音助手（实时对话）

```python
# 基于 main.py 的语音助手
# 1. 配置 config.py 中的 system_role 为助手角色
# 2. 运行 python main.py
# 3. 说 "你好豆包" 开始对话
```

### 示例3：语音问答系统

```bash
# 步骤1：录制问题完成采样频率的额切换
python audio_converter.py record -o questions/q1.wav

# 步骤2：获取回答
python .\Filedialogue.py .\crosstalk_subset_1_16khz.wav -o output_4.wav  # 修改代码中的input_file路径

# 步骤3：播放回答
# Windows: start output.wav
# macOS: afplay output.wav
```

## ❓ 常见问题

### 实时对话相关

**Q: 为什么会有回声？**
- A: 请使用耳机，或调整麦克风和扬声器的位置

**Q: 对话经常被打断？**
- A: 可能是环境噪音导致，请在安静环境使用

### 非实时问答相关

**Q: 支持哪些音频格式？**
- A: 原生支持WAV格式，其他格式请先用 `audio_converter.py` 转换

**Q: 批量处理失败怎么办？**
- A: 查看生成的 `batch_result_*.json` 文件，了解具体哪个文件失败

### 通用问题

**Q: 连接失败怎么办？**
- A: 检查网络连接和API密钥配置

**Q: 音频没有声音？**
- A: 确保音频文件不是静音，且格式正确（16kHz）

## 🚀 性能优化建议

### 实时对话优化
- 使用有线网络连接减少延迟
- 选择高质量麦克风设备
- 在安静环境中使用

### 非实时问答优化
- 批量处理时合理设置并发数
- 预先转换好音频格式
- 使用SSD存储提高文件读写速度

## 📝 注意事项

- 请遵守API使用限制，避免频繁请求
- 妥善保管API密钥，不要提交到公开仓库
- 实时对话模式需要稳定的网络连接

## 🤝 贡献指南

欢迎贡献代码和建议！

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/YourFeature`)
3. 提交修改 (`git commit -m 'Add YourFeature'`)
4. 推送分支 (`git push origin feature/YourFeature`)
5. 创建 Pull Request

## 📄 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情

## 🙏 致谢

- 感谢字节跳动提供的豆包AI服务
- 感谢开源社区的支持

---
