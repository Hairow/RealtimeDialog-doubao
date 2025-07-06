import asyncio
import uuid
import wave
import struct
from typing import Dict, Any, List
import json
import time
from pathlib import Path

import config
from realtime_dialog_client import RealtimeDialogClient


class FileBasedDialog:
    """基于文件的语音对话类"""

    def __init__(self, ws_config: Dict[str, Any]):
        self.session_id = str(uuid.uuid4())
        self.client = RealtimeDialogClient(config=ws_config, session_id=self.session_id)
        self.audio_chunks: List[bytes] = []  # 存储接收到的音频数据块
        self.is_session_finished = False

    async def process_audio_file(self, input_file: str, output_file: str) -> Dict[str, Any]:
        """
        处理音频文件并获取回复

        Args:
            input_file: 输入音频文件路径 (WAV格式, 16kHz, 单声道)
            output_file: 输出音频文件路径 (将保存为 WAV格式, 24kHz, 单声道)

        Returns:
            Dict[str, Any]: 包含处理结果的字典
        """
        start_time = time.time()
        result = {
            "session_id": self.session_id,
            "input_file": input_file,
            "output_file": output_file,
            "status": "processing",
            "error": None,
            "duration": 0,
            "response_chunks": 0
        }

        try:
            # 连接到服务器
            print(f"[1/5] 连接到豆包服务器...")
            await self.client.connect()

            # 读取输入音频文件
            print(f"[2/5] 读取输入音频文件: {input_file}")
            audio_data = self._read_audio_file(input_file)

            # 创建接收任务
            receive_task = asyncio.create_task(self._receive_loop())

            # 发送音频数据
            print(f"[3/5] 发送音频数据到服务器...")
            await self._send_audio_data(audio_data)

            # 等待响应完成
            print(f"[4/5] 等待豆包回复...")
            await self._wait_for_response()

            # 保存输出音频
            print(f"[5/5] 保存输出音频到: {output_file}")
            self._save_audio_file(output_file)

            # 清理
            await self.client.finish_session()

            # 等待会话真正结束
            timeout = 10  # 10秒超时
            start_wait = time.time()
            while not self.is_session_finished and (time.time() - start_wait) < timeout:
                await asyncio.sleep(0.1)

            await self.client.finish_connection()
            await self.client.close()

            # 取消接收任务
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass

            result["status"] = "success"
            result["duration"] = time.time() - start_time
            result["response_chunks"] = len(self.audio_chunks)
            result["logid"] = self.client.logid

            print(f"\n✅ 处理完成!")
            print(f"   - 会话ID: {self.session_id}")
            print(f"   - 日志ID: {self.client.logid}")
            print(f"   - 耗时: {result['duration']:.2f}秒")
            print(f"   - 音频块数: {result['response_chunks']}")

        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            result["duration"] = time.time() - start_time
            print(f"\n❌ 处理失败: {e}")

        return result

    def _read_audio_file(self, file_path: str) -> bytes:
        """
        读取WAV音频文件

        Args:
            file_path: 音频文件路径

        Returns:
            bytes: PCM音频数据
        """
        with wave.open(file_path, 'rb') as wf:
            # 验证音频格式
            if wf.getnchannels() != 1:
                raise ValueError(f"音频必须是单声道，当前是 {wf.getnchannels()} 声道")
            if wf.getframerate() != 16000:
                raise ValueError(f"音频采样率必须是 16000Hz，当前是 {wf.getframerate()}Hz")
            if wf.getsampwidth() != 2:
                raise ValueError(f"音频必须是 16-bit，当前是 {wf.getsampwidth() * 8}-bit")

            # 读取所有音频数据
            frames = wf.readframes(wf.getnframes())
            return frames

    async def _send_audio_data(self, audio_data: bytes) -> None:
        """
        分块发送音频数据

        Args:
            audio_data: 完整的音频数据
        """
        chunk_size = config.input_audio_config["chunk"]
        total_chunks = len(audio_data) // chunk_size

        for i in range(0, len(audio_data), chunk_size):
            chunk = audio_data[i:i + chunk_size]
            if len(chunk) < chunk_size:
                # 如果是最后一块且不足chunk_size，用静音填充
                chunk = chunk + b'\x00' * (chunk_size - len(chunk))

            await self.client.task_request(chunk)

            # 模拟实时发送的速率 (16000采样率，每块3200字节=1600个样本=0.1秒)
            await asyncio.sleep(0.1)

            # 显示进度
            current_chunk = min(i // chunk_size + 1, total_chunks)
            print(f"\r   发送进度: {current_chunk}/{total_chunks} ({current_chunk / total_chunks * 100:.1f}%)", end='')

        print()  # 换行

    async def _receive_loop(self) -> None:
        """接收服务器响应的循环"""
        try:
            while True:
                response = await self.client.receive_server_response()
                self._handle_server_response(response)

                # 检查会话是否结束
                if 'event' in response and response['event'] in [152, 153]:
                    print(f"\n   收到会话结束事件: {response['event']}")
                    self.is_session_finished = True
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"\n   接收消息错误: {e}")

    def _handle_server_response(self, response: Dict[str, Any]) -> None:
        """处理服务器响应"""
        if not response:
            return

        if response['message_type'] == 'SERVER_ACK' and isinstance(response.get('payload_msg'), bytes):
            # 收到音频数据
            self.audio_chunks.append(response['payload_msg'])
            print(f"\r   已接收音频块: {len(self.audio_chunks)}", end='')

        elif response['message_type'] == 'SERVER_FULL_RESPONSE':
            print(f"\n   服务器消息: {response.get('payload_msg', {})}")

            # 处理打断事件
            if response.get('event') == 450:
                print(f"   收到打断事件，清空已接收的音频")
                self.audio_chunks.clear()

        elif response['message_type'] == 'SERVER_ERROR':
            print(f"\n   服务器错误: {response.get('payload_msg', {})}")
            raise Exception(f"服务器错误: {response.get('payload_msg', {})}")

    async def _wait_for_response(self) -> None:
        """等待响应完成"""
        # 等待一段时间确保接收到所有响应
        # 可以根据实际情况调整等待策略
        max_wait_time = 30  # 最长等待30秒
        start_time = time.time()
        last_chunk_count = 0
        stable_count = 0

        while (time.time() - start_time) < max_wait_time:
            current_chunk_count = len(self.audio_chunks)

            # 如果音频块数量稳定不变，说明可能已经接收完毕
            if current_chunk_count == last_chunk_count and current_chunk_count > 0:
                stable_count += 1
                if stable_count >= 10:  # 连续1秒没有新数据
                    print(f"\n   音频接收完成，共 {current_chunk_count} 块")
                    break
            else:
                stable_count = 0

            last_chunk_count = current_chunk_count
            await asyncio.sleep(0.1)

    def _save_audio_file(self, file_path: str) -> None:
        """
        保存音频数据到WAV文件

        Args:
            file_path: 输出文件路径
        """
        if not self.audio_chunks:
            raise ValueError("没有接收到音频数据")

        # 合并所有音频块
        audio_data = b''.join(self.audio_chunks)

        # 将float32转换为int16 (豆包返回的是float32格式)
        float_data = struct.unpack(f'{len(audio_data) // 4}f', audio_data)
        int_data = [int(max(-32768, min(32767, sample * 32768))) for sample in float_data]
        pcm_data = struct.pack(f'{len(int_data)}h', *int_data)

        # 保存为WAV文件
        with wave.open(file_path, 'wb') as wf:
            wf.setnchannels(1)  # 单声道
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(24000)  # 豆包返回的是24kHz
            wf.writeframes(pcm_data)

        print(f"   音频时长: {len(pcm_data) / 2 / 24000:.2f}秒")


async def main():
    """主函数示例"""
    # 创建对话实例
    dialog = FileBasedDialog(config.ws_connect_config)

    # 处理音频文件
    input_file = "input.wav"  # 请确保这是16kHz, 16-bit, 单声道的WAV文件
    output_file = "output.wav"

    # 检查输入文件是否存在
    if not Path(input_file).exists():
        print(f"错误: 输入文件 '{input_file}' 不存在!")
        print("请确保有一个16kHz, 16-bit, 单声道的WAV文件作为输入")
        return

    # 执行对话
    result = await dialog.process_audio_file(input_file, output_file)

    # 保存处理结果
    with open("dialog_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n处理结果已保存到: dialog_result.json")


if __name__ == "__main__":
    asyncio.run(main())