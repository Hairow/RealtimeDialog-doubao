"""豆包英语玩伴（阶段2）：回车触发 + 半双工实时对话

用法：
    python companion.py
    - 按一次回车：结束本次会话（相当于对豆包说"再见"）
    - Ctrl+C：退出程序
    - 豆包说话期间自动闭麦（半双工），说完自动恢复拾音
"""
import asyncio
import queue
import signal
import threading
import time
import uuid

import pyaudio

import config
from realtime_dialog_client import RealtimeDialogClient

IN_RATE = config.input_audio_config["sample_rate"]     # 16000
OUT_RATE = config.output_audio_config["sample_rate"]   # 24000
CHUNK = config.input_audio_config["chunk"]             # 3200 字节 = 100ms
IDLE_EXIT_SECONDS = 30  # 服务端 30 秒无响应自动结束会话


class Companion:
    def __init__(self) -> None:
        self.session_id = str(uuid.uuid4())
        self.client = RealtimeDialogClient(config=config.ws_connect_config,
                                           session_id=self.session_id)
        self.pa = pyaudio.PyAudio()
        self.out_stream = self.pa.open(
            format=pyaudio.paInt16, channels=1, rate=OUT_RATE,
            output=True, frames_per_buffer=3200)
        self.audio_q: "queue.Queue[bytes]" = queue.Queue()
        self.doubao_speaking = False   # 事件层：豆包正在合成语音
        self.running = True            # 程序总开关
        self.session_done = False      # 本次会话结束
        self.last_response_time = time.time()
        self.player = threading.Thread(target=self._play_loop, daemon=True)
        self.player.start()
        signal.signal(signal.SIGINT, self._sigint)

    # ---------- 播放 ----------
    def _play_loop(self) -> None:
        while True:
            try:
                data = self.audio_q.get(timeout=0.2)
            except queue.Empty:
                if not self.running and self.audio_q.empty():
                    break
                continue
            self.out_stream.write(data)

    # ---------- 信号 ----------
    def _sigint(self, sig, frame) -> None:
        print("\n收到 Ctrl+C，退出...")
        self.running = False
        self.session_done = True

    # ---------- 接收 ----------
    async def receive_loop(self) -> None:
        while self.running and not self.session_done:
            try:
                resp = await asyncio.wait_for(
                    self.client.receive_server_response(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"\n接收中断: {e}")
                break
            self.last_response_time = time.time()
            if resp == {}:
                continue
            event = resp.get("event")
            if resp.get("message_type") == "SERVER_ACK" and isinstance(
                    resp.get("payload_msg"), bytes):
                self.audio_q.put(resp["payload_msg"])
            elif resp.get("message_type") == "SERVER_ERROR":
                print(f"\n[服务端错误] {resp.get('payload_msg')}")
                self.session_done = True
            elif event == 451:
                for r in resp.get("payload_msg", {}).get("results", []):
                    if not r.get("is_interim") and r.get("text"):
                        print(f"\r孩子说: {r['text']}")
            elif event == 550:
                text = resp.get("payload_msg", {}).get("text", "")
                if text:
                    print(f"豆包: {text}")
            elif event == 350:   # TTSSentenceStart
                self.doubao_speaking = True
            elif event == 359:   # TTSEnded：本轮语音合成结束
                self.doubao_speaking = False
            elif event == 152 or event == 153:
                print(f"\n[会话结束] event={event}")
                self.session_done = True
            elif event not in (450, 451, 50, 150, 154, 52):
                print(f"[事件 {event}] {resp.get('payload_msg')}")

    # ---------- 麦克风（半双工） ----------
    async def mic_loop(self) -> None:
        in_stream = self.pa.open(
            format=pyaudio.paInt16, channels=1, rate=IN_RATE,
            input=True, frames_per_buffer=CHUNK)
        print("麦克风已开，直接说话即可。豆包说话时会自动闭麦。")
        muted = False
        while self.running and not self.session_done:
            data = in_stream.read(CHUNK, exception_on_overflow=False)
            # 半双工：豆包在说话（合成中或还有音频没放完）就不上行
            busy = self.doubao_speaking or not self.audio_q.empty()
            if busy:
                if not muted:
                    print("…（豆包说话中，闭麦）")
                    muted = True
                continue
            if muted:
                print("…（恢复拾音）")
                muted = False
                # 多吞掉一小段，避免回声尾巴
                in_stream.read(CHUNK * 2, exception_on_overflow=False)
            await self.client.task_request(data)
        in_stream.stop_stream()
        in_stream.close()

    # ---------- 主流程 ----------
    async def run(self) -> None:
        print("=" * 50)
        print("豆包英语玩伴（回车版）")
        print("  直接对 Mac 说话，全程英语陪聊")
        print("  按回车 = 结束本次会话；Ctrl+C = 退出")
        print("=" * 50)
        await self.client.connect()
        loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(self.receive_loop()),
                 asyncio.create_task(self.mic_loop())]
        try:
            while self.running and not self.session_done:
                # 30 秒无任何服务端消息自动结束
                if time.time() - self.last_response_time > IDLE_EXIT_SECONDS:
                    print("\n30 秒没有对话，自动结束本次会话。")
                    break
                # 回车触发：结束会话
                got = await loop.run_in_executor(None, input)
                if got == "" and not self.session_done:
                    print("结束本次会话...")
                    break
        finally:
            self.session_done = True
            await self.client.finish_session()
            # 等收尾事件/播放完
            deadline = time.time() + 5
            while time.time() < deadline and (not self.audio_q.empty()):
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.8)  # 让最后一小段播完
            for t in tasks:
                t.cancel()
            await self.client.finish_connection()
            await self.client.close()
            self.out_stream.stop_stream()
            self.out_stream.close()
            self.pa.terminate()
            print("已退出。")


if __name__ == "__main__":
    asyncio.run(Companion().run())
