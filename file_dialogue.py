import asyncio
import uuid
import wave
import queue
import threading
import time
from typing import Optional, Dict, Any
from pathlib import Path

import config
from realtime_dialog_client import RealtimeDialogClient
import numpy as np
import pyaudio


class FileBasedDialog:
    """基于文件的对话会话管理类"""

    def __init__(self, ws_config: Dict[str, Any]):
        self.session_id = str(uuid.uuid4())
        self.client = RealtimeDialogClient(config=ws_config, session_id=self.session_id)

        self.is_running = True
        self.is_session_finished = False

        # 用于收集音频数据的队列
        self.audio_queue = queue.Queue()
        self.collected_audio = []

        # 音频收集线程
        self.is_collecting = True
        self.collector_thread = None

        # 接收任务
        self.receive_task = None

        # 最近一次收到服务端消息的时间（用于判断静默超时）
        self.last_response_time = time.time()

    def start_audio_collector(self):
        """启动音频收集线程"""
        self.collector_thread = threading.Thread(target=self._audio_collector_thread)
        self.collector_thread.daemon = True
        self.collector_thread.start()

    def _audio_collector_thread(self):
        """音频收集线程，替代原来的播放线程"""
        while self.is_collecting:
            try:
                # 从队列获取音频数据
                audio_data = self.audio_queue.get(timeout=0.1)
                if audio_data is not None:
                    self.collected_audio.append(audio_data)
            except queue.Empty:
                continue
            except Exception as e:
                if self.is_collecting:  # 只在仍在收集时报错
                    print(f"\n音频收集错误: {e}")
                time.sleep(0.01)

    def handle_server_response(self, response: Dict[str, Any]) -> None:
        """处理服务器响应"""
        if response == {}:
            return

        self.last_response_time = time.time()

        # 调试信息：打印响应类型和内容
        print(f"\n[DEBUG] 响应类型: {response.get('message_type')}, "
              f"payload类型: {type(response.get('payload_msg'))}, "
              f"event: {response.get('event')}")

        if response['message_type'] == 'SERVER_ACK' and isinstance(response.get('payload_msg'), bytes):
            # 将音频数据放入队列
            audio_data = response['payload_msg']
            self.audio_queue.put(audio_data)
            print(f"[音频数据] 大小: {len(audio_data)} 字节")
        elif response['message_type'] == 'SERVER_FULL_RESPONSE':
            print(f"\n[完整响应] event: {response.get('event')}, "
                  f"payload: {response.get('payload_msg')}")
            if response['event'] == 450:
                print(f"清空缓存音频: {response['session_id']}")
                # 清空队列
                while not self.audio_queue.empty():
                    try:
                        self.audio_queue.get_nowait()
                    except queue.Empty:
                        break
        elif response['message_type'] == 'SERVER_ERROR':
            print(f"\n[错误] {response['payload_msg']}")
            self.is_session_finished = True
            raise Exception("服务器错误")
        else:
            print(f"\n[其他响应] {response}")

    async def receive_loop(self):
        """接收服务器响应循环"""
        try:
            while self.is_running and not self.is_session_finished:
                try:
                    # 使用超时避免无限等待
                    response = await asyncio.wait_for(
                        self.client.receive_server_response(),
                        timeout=1.0
                    )
                    self.handle_server_response(response)
                    if 'event' in response and response['event'] in [152, 153]:
                        print(f"\n收到会话结束事件: {response['event']}")
                        self.is_session_finished = True
                        break
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"接收消息错误: {e}")
                    break
        except asyncio.CancelledError:
            print("接收任务已取消")
        except Exception as e:
            print(f"接收循环错误: {e}")
        finally:
            print("接收循环结束")

    async def process_file_input(self, input_file: str) -> None:
        print(f"读取音频文件: {input_file}")

        with wave.open(input_file, 'rb') as wf:
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()

            if nchannels != 1 or sampwidth != 2 or framerate != 16000:
                raise ValueError(f"不符合要求: {nchannels}通道, {sampwidth * 8}bit, {framerate}Hz")

            frame_size = sampwidth * nchannels
            chunk_bytes = config.input_audio_config["chunk"]
            frames_per_chunk = chunk_bytes // frame_size

            chunk_count = 0
            while self.is_running and not self.is_session_finished:
                audio_data = wf.readframes(frames_per_chunk)
                if not audio_data:
                    break

                if len(audio_data) < chunk_bytes:
                    # 补零或直接跳过
                    audio_data += b'\x00' * (chunk_bytes - len(audio_data))

                await self.client.task_request(audio_data)

                chunk_count += 1
                print(f"\r发送第 {chunk_count} 块: {len(audio_data)} 字节", end='')

                # 延迟模拟
                chunk_duration = len(audio_data) / (frame_size * framerate)
                await asyncio.sleep(chunk_duration * 1.05)  # 留裕量

        print(f"\n音频发送完成，共发送 {chunk_count} 块")

    def save_collected_audio(self, output_file: str) -> None:
        """保存收集的音频到文件"""
        if not self.collected_audio:
            print("没有音频数据可保存")
            return

        # 合并所有音频数据
        combined_audio = b''.join(self.collected_audio)

        # 处理32位浮点数音频
        if config.output_audio_config["bit_size"] == pyaudio.paFloat32:
            # 将32位浮点数转换为16位整数以保存为标准WAV
            float_data = np.frombuffer(combined_audio, dtype=np.float32)
            float_data = np.clip(float_data, -1.0, 1.0)
            int_data = (float_data * 32767).astype(np.int16)
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
        with wave.open(output_file, 'wb') as wf:
            wf.setnchannels(config.output_audio_config["channels"])
            wf.setsampwidth(sampwidth)
            wf.setframerate(config.output_audio_config["sample_rate"])
            wf.writeframes(audio_bytes)

        print(f"音频已保存到文件: {output_file}")
        print(f"音频参数: {config.output_audio_config['channels']}通道, "
              f"{config.output_audio_config['sample_rate']}Hz采样率, "
              f"{sampwidth * 8}位深度")
        print(f"原始数据大小: {len(combined_audio)} 字节")
        print(f"保存文件大小: {len(audio_bytes)} 字节")

    async def cleanup(self):
        """清理资源"""
        print("开始清理资源...")

        # 停止标志
        self.is_running = False
        self.is_session_finished = True
        self.is_collecting = False

        # 取消接收任务
        if self.receive_task and not self.receive_task.done():
            self.receive_task.cancel()
            try:
                await self.receive_task
            except asyncio.CancelledError:
                pass

        # 等待收集线程结束
        if self.collector_thread and self.collector_thread.is_alive():
            self.collector_thread.join(timeout=2.0)
            if self.collector_thread.is_alive():
                print("警告：收集线程未能正常结束")

        # 清空队列
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

        print("资源清理完成")

    async def process_audio_file(self, input_file: str, output_file: str) -> None:
        """处理音频文件的主方法"""
        try:
            # 启动音频收集线程
            self.start_audio_collector()

            # 连接服务器
            print("连接服务器...")
            await self.client.connect()

            # 启动接收任务
            self.receive_task = asyncio.create_task(self.receive_loop())

            # 处理文件输入
            await self.process_file_input(input_file)

            # 等待服务器响应：豆包思考+合成需要几秒，静默超过 6 秒才视为结束，最多等 60 秒
            print("等待服务器响应...")
            max_wait = 60
            idle_timeout = 6  # 连续 6 秒无任何服务端消息则结束
            wait_start = time.time()

            while not self.is_session_finished and (time.time() - wait_start) < max_wait:
                await asyncio.sleep(0.2)
                if time.time() - self.last_response_time > idle_timeout:
                    break

            # 结束会话
            print("结束会话...")
            try:
                await self.client.finish_session()
                # 等待会话结束确认
                for _ in range(20):  # 最多等待2秒
                    if self.is_session_finished:
                        break
                    await asyncio.sleep(0.1)
            except Exception as e:
                print(f"结束会话时出错: {e}")

            # 关闭连接
            try:
                await self.client.finish_connection()
                await asyncio.sleep(0.1)
                await self.client.close()
            except Exception as e:
                print(f"关闭连接时出错: {e}")

            # 保存音频
            self.save_collected_audio(output_file)

            print(f"\n处理完成！")
            print(f"请求logid: {self.client.logid}")

        except Exception as e:
            print(f"会话错误: {e}")
            raise
        finally:
            # 确保清理资源
            await self.cleanup()


async def main():
    """示例用法"""
    import argparse

    parser = argparse.ArgumentParser(description='基于文件的音频对话')
    parser.add_argument('input', help='输入音频文件路径（需要16kHz, 16bit, 单声道）')
    parser.add_argument('-o', '--output', default='output_reply.wav', help='输出音频文件路径')

    args = parser.parse_args()

    # 检查输入文件
    if not Path(args.input).exists():
        print(f"错误: 输入文件不存在: {args.input}")
        return

    # 创建对话实例并处理
    dialog = FileBasedDialog(config.ws_connect_config)
    try:
        await dialog.process_audio_file(args.input, args.output)
    finally:
        # 确保清理
        await dialog.cleanup()
        # 给一点时间让所有资源释放
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())