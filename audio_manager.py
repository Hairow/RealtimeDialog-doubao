import asyncio
import uuid
import queue
import threading
import time
from typing import Optional, Dict, Any, List
import wave
import pyaudio
import signal
from dataclasses import dataclass
from datetime import datetime
import struct
import numpy as np

import config
from realtime_dialog_client import RealtimeDialogClient


@dataclass
class AudioConfig:
    """音频配置数据类"""
    format: str
    bit_size: int
    channels: int
    sample_rate: int
    chunk: int


class AudioDeviceManager:
    """音频设备管理类，处理音频输入输出"""

    def __init__(self, input_config: AudioConfig, output_config: AudioConfig):
        self.input_config = input_config
        self.output_config = output_config
        self.pyaudio = pyaudio.PyAudio()
        self.input_stream: Optional[pyaudio.Stream] = None
        self.output_stream: Optional[pyaudio.Stream] = None

    def open_input_stream(self) -> pyaudio.Stream:
        """打开音频输入流"""
        # p = pyaudio.PyAudio()
        self.input_stream = self.pyaudio.open(
            format=self.input_config.bit_size,
            channels=self.input_config.channels,
            rate=self.input_config.sample_rate,
            input=True,
            frames_per_buffer=self.input_config.chunk
        )
        return self.input_stream

    def open_output_stream(self) -> pyaudio.Stream:
        """打开音频输出流"""
        self.output_stream = self.pyaudio.open(
            format=self.output_config.bit_size,
            channels=self.output_config.channels,
            rate=self.output_config.sample_rate,
            output=True,
            frames_per_buffer=self.output_config.chunk
        )
        return self.output_stream

    def cleanup(self) -> None:
        """清理音频设备资源"""
        for stream in [self.input_stream, self.output_stream]:
            if stream:
                stream.stop_stream()
                stream.close()
        self.pyaudio.terminate()


class DialogSession:
    """对话会话管理类"""

    def __init__(self, ws_config: Dict[str, Any]):
        self.session_id = str(uuid.uuid4())
        self.client = RealtimeDialogClient(config=ws_config, session_id=self.session_id)
        self.audio_device = AudioDeviceManager(
            AudioConfig(**config.input_audio_config),
            AudioConfig(**config.output_audio_config)
        )

        self.is_running = True
        self.is_session_finished = False

        signal.signal(signal.SIGINT, self._keyboard_signal)
        # 初始化音频队列和输出流
        self.audio_queue = queue.Queue()
        self.output_stream = self.audio_device.open_output_stream()

        # 添加音频数据收集列表
        self.collected_audio_data: List[bytes] = []
        self.audio_data_lock = threading.Lock()

        # 启动播放线程
        self.is_recording = True
        self.is_playing = True
        self.player_thread = threading.Thread(target=self._audio_player_thread)
        self.player_thread.daemon = True
        self.player_thread.start()

    def _audio_player_thread(self):
        """音频播放线程"""
        while self.is_playing:
            try:
                # 从队列获取音频数据
                audio_data = self.audio_queue.get(timeout=1.0)
                if audio_data is not None:
                    self.output_stream.write(audio_data)
                    # 收集音频数据
                    with self.audio_data_lock:
                        self.collected_audio_data.append(audio_data)
            except queue.Empty:
                # 队列为空时等待一小段时间
                time.sleep(0.1)
            except Exception as e:
                print(f"音频播放错误: {e}")
                time.sleep(0.1)

    def save_collected_audio(self, filename: Optional[str] = None) -> str:
        """保存收集的音频数据到文件"""
        if not filename:
            # 生成带时间戳的文件名
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"response_audio_{timestamp}.wav"

        with self.audio_data_lock:
            if not self.collected_audio_data:
                print("没有音频数据需要保存")
                return ""

            # 合并所有音频数据
            combined_audio = b''.join(self.collected_audio_data)

            # 处理32位浮点数音频
            if config.output_audio_config["bit_size"] == pyaudio.paFloat32:
                # 将32位浮点数转换为16位整数以保存为标准WAV
                # 首先将字节数据转换为numpy数组
                float_data = np.frombuffer(combined_audio, dtype=np.float32)

                # 将浮点数（范围-1.0到1.0）转换为16位整数（范围-32768到32767）
                # 限制在-1.0到1.0范围内避免溢出
                float_data = np.clip(float_data, -1.0, 1.0)
                int_data = (float_data * 32767).astype(np.int16)

                # 转换回字节
                audio_bytes = int_data.tobytes()
                sampwidth = 2  # 16位

                print("已将32位浮点音频转换为16位整数格式")
            else:
                # 其他格式的处理
                audio_bytes = combined_audio
                bit_size = config.output_audio_config["bit_size"]
                if bit_size == pyaudio.paInt16:
                    sampwidth = 2
                elif bit_size == pyaudio.paInt24:
                    sampwidth = 3
                elif bit_size == pyaudio.paInt32:
                    sampwidth = 4
                elif bit_size == pyaudio.paInt8:
                    sampwidth = 1
                else:
                    sampwidth = 2
                    print(f"警告：未知的音频位深度 {bit_size}，使用默认16位")

            # 保存为WAV文件
            with wave.open(filename, 'wb') as wf:
                wf.setnchannels(config.output_audio_config["channels"])
                wf.setsampwidth(sampwidth)
                wf.setframerate(config.output_audio_config["sample_rate"])
                wf.writeframes(audio_bytes)

            print(f"音频已保存到文件: {filename}")
            print(f"音频参数: {config.output_audio_config['channels']}通道, "
                  f"{config.output_audio_config['sample_rate']}Hz采样率, "
                  f"{sampwidth * 8}位深度")
            print(f"原始数据大小: {len(combined_audio)} 字节")
            print(f"保存文件大小: {len(audio_bytes)} 字节")

            # 清空已保存的音频数据
            self.collected_audio_data.clear()

        return filename

    def handle_server_response(self, response: Dict[str, Any]) -> None:
        if response == {}:
            return
        """处理服务器响应"""
        if response['message_type'] == 'SERVER_ACK' and isinstance(response.get('payload_msg'), bytes):
            # print(f"\n接收到音频数据: {len(response['payload_msg'])} 字节")
            audio_data = response.get('payload_msg')

            # 调试：打印第一次接收到的音频数据信息
            if not hasattr(self, '_first_audio_logged'):
                print(f"首次音频数据信息:")
                print(f"  - 数据长度: {len(audio_data)} 字节")
                print(f"  - 前16字节: {audio_data[:16].hex() if len(audio_data) >= 16 else audio_data.hex()}")
                self._first_audio_logged = True

            self.audio_queue.put(audio_data)
        elif response['message_type'] == 'SERVER_FULL_RESPONSE':
            print(f"服务器响应: {response}")
            if response['event'] == 450:
                print(f"清空缓存音频: {response['session_id']}")
                # 在清空队列前，先保存当前收集的音频
                if self.collected_audio_data:
                    self.save_collected_audio()

                while not self.audio_queue.empty():
                    try:
                        self.audio_queue.get_nowait()
                    except queue.Empty:
                        continue

                # 清空已收集的音频数据
                with self.audio_data_lock:
                    self.collected_audio_data.clear()

        elif response['message_type'] == 'SERVER_ERROR':
            print(f"服务器错误: {response['payload_msg']}")
            raise Exception("服务器错误")

    def _keyboard_signal(self, sig, frame):
        print(f"receive keyboard Ctrl+C")
        self.is_recording = False
        self.is_playing = False
        self.is_running = False

    async def receive_loop(self):
        try:
            while True:
                response = await self.client.receive_server_response()
                self.handle_server_response(response)
                if 'event' in response and (response['event'] == 152 or response['event'] == 153):
                    print(f"receive session finished event: {response['event']}")
                    self.is_session_finished = True
                    break
        except asyncio.CancelledError:
            print("接收任务已取消")
        except Exception as e:
            print(f"接收消息错误: {e}")

    async def process_microphone_input(self) -> None:
        """处理麦克风输入"""
        stream = self.audio_device.open_input_stream()
        print("已打开麦克风，请讲话...")

        while self.is_recording:
            try:
                # 添加exception_on_overflow=False参数来忽略溢出错误
                audio_data = stream.read(config.input_audio_config["chunk"], exception_on_overflow=False)
                save_pcm_to_wav(audio_data, "output.wav")
                await self.client.task_request(audio_data)
                await asyncio.sleep(0.01)  # 避免CPU过度使用
            except Exception as e:
                print(f"读取麦克风数据出错: {e}")
                await asyncio.sleep(0.1)  # 给系统一些恢复时间

    async def start(self) -> None:
        """启动对话会话"""
        try:
            await self.client.connect()
            asyncio.create_task(self.process_microphone_input())
            asyncio.create_task(self.receive_loop())

            while self.is_running:
                await asyncio.sleep(0.1)

            await self.client.finish_session()
            while not self.is_session_finished:
                await asyncio.sleep(0.1)

            # 等待音频队列播放完毕
            print("等待音频播放完毕...")
            while not self.audio_queue.empty():
                await asyncio.sleep(0.1)

            # 再等待一小段时间确保最后的音频数据被播放和收集
            await asyncio.sleep(1.0)

            # 保存收集的音频数据
            if self.collected_audio_data:
                saved_file = self.save_collected_audio()
                print(f"会话音频已保存: {saved_file}")

            # 停止播放线程
            self.is_playing = False
            self.player_thread.join(timeout=2.0)

            await self.client.finish_connection()
            await asyncio.sleep(0.1)
            await self.client.close()
            print(f"dialog request logid: {self.client.logid}")
        except Exception as e:
            print(f"会话错误: {e}")
            # 发生错误时也尝试保存已收集的音频
            if self.collected_audio_data:
                self.save_collected_audio(f"error_audio_{self.session_id}.wav")
        finally:
            self.audio_device.cleanup()


def save_pcm_to_wav(pcm_data: bytes, filename: str) -> None:
    """保存PCM数据为WAV文件"""
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(config.input_audio_config["channels"])
        wf.setsampwidth(2)  # paInt16 = 2 bytes
        wf.setframerate(config.input_audio_config["sample_rate"])
        wf.writeframes(pcm_data)
