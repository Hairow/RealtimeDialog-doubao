"""豆包英语玩伴（阶段3）：喊"豆包豆包"唤醒 + 半双工实时对话

用法：
    python companion.py            # 语音唤醒模式（默认）
    python companion.py --enter    # 回车触发模式（阶段2原版）
    - 会话中：喊"再见/别说了/停下…"任一结束词即可结束
    - 待机时：喊"豆包豆包"重新开始
    - Ctrl+C：退出程序
    - 豆包说话期间自动闭麦（半双工），说完自动恢复拾音
"""
import asyncio
import math
import queue
import signal
import sys
import threading
import time
import uuid

import numpy as np
import pyaudio

import config
from realtime_dialog_client import RealtimeDialogClient
from wakeup import SAMPLE_RATE as IN_RATE, WAKE_LABEL, WakeSpotter

OUT_RATE = config.output_audio_config["sample_rate"]   # 24000
CHUNK = config.input_audio_config["chunk"]             # 3200 字节 = 100ms
IDLE_EXIT_SECONDS = 30  # 服务端 30 秒无响应自动结束会话


def _chime_bytes() -> bytes:
    """一声短促的"叮"，提示唤醒成功。"""
    rate, dur = 24000, 0.18
    n = int(rate * dur)
    t = np.arange(n)
    fade = np.minimum(1.0, np.minimum(t, n - 1 - t) / (0.02 * rate))
    wave = 0.5 * np.sin(2 * math.pi * 880 * t / rate) * fade
    return (wave * 32767).astype(np.int16).tobytes()


class Companion:
    def __init__(self, enter_mode: bool = False, debug: bool = False) -> None:
        self.enter_mode = enter_mode
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
        self.kws: WakeSpotter | None = None  # 仅唤醒模式加载
        self.debug = debug              # --debug 时保存发给服务器的原声
        self._debug_buf: list[bytes] = []
        # 全程共用一个输入流（避免待机/会话各开一个的冲突）
        self.in_stream, self.in_rate = self.open_input()
        self.player = threading.Thread(target=self._play_loop, daemon=True)
        self.player.start()
        signal.signal(signal.SIGINT, self._sigint)

    # ---------- 音频流 ----------
    def open_input(self):
        """优先 16k 直开；设备不支持则按默认采样率开、读取时重采样。"""
        try:
            return self.pa.open(format=pyaudio.paInt16, channels=1,
                                rate=IN_RATE, input=True,
                                frames_per_buffer=CHUNK), IN_RATE
        except OSError:
            info = self.pa.get_default_input_device_info()
            rate = int(info["defaultSampleRate"])
            print(f"[提示] 设备不支持 16k，改用 {rate}Hz 采集并软件重采样")
            return self.pa.open(format=pyaudio.paInt16, channels=1,
                                rate=rate, input=True,
                                frames_per_buffer=CHUNK), rate

    def read_chunk(self, in_stream=None, rate: int | None = None) -> bytes:
        """读 100ms 音频，必要时重采样到 16k。"""
        in_stream = in_stream or self.in_stream
        rate = rate or self.in_rate
        raw = in_stream.read(int(CHUNK * rate / IN_RATE),
                             exception_on_overflow=False)
        if rate == IN_RATE:
            return raw
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
        target = len(x) * IN_RATE // rate
        idx = np.linspace(0, len(x) - 1, target)
        return np.interp(idx, np.arange(len(x)), x).astype(np.int16).tobytes()

    def drain_input(self, seconds: float = 0.5) -> None:
        """丢掉输入流里积压的旧音频（会话开始前/结束后清尾巴）。"""
        n = int(seconds * 10)  # 每 chunk = 100ms
        for _ in range(n):
            self.read_chunk()

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

    def chime(self) -> None:
        self.out_stream.write(_chime_bytes())

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
            try:
                event = resp.get("event")
                if resp.get("message_type") == "SERVER_ACK" and isinstance(
                        resp.get("payload_msg"), bytes):
                    self.audio_q.put(resp["payload_msg"])
                elif resp.get("message_type") == "SERVER_ERROR":
                    print(f"\n[服务端错误] {resp.get('payload_msg')}")
                    self.session_done = True
                elif event == 451:
                    payload = resp.get("payload_msg") or {}
                    for r in payload.get("results", []):
                        if not r.get("is_interim") and r.get("text"):
                            print(f"\r孩子说: {r['text']}")
                elif event == 550:
                    payload = resp.get("payload_msg") or {}
                    text = payload.get("text", "") if isinstance(payload, dict) else ""
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
            except Exception as e:
                # 单条消息解析失败只跳过，不让接收循环静默死亡
                print(f"\n[解析异常 event={resp.get('event')}] {e}")

    # ---------- 会话中的麦克风（半双工 + 结束词监听） ----------
    async def mic_loop(self) -> None:
        print("麦克风已开，直接说话即可。豆包说话时会自动闭麦。")
        muted = False
        chunks_sent = 0
        while self.running and not self.session_done:
            data = self.read_chunk()
            if self.debug:
                self._debug_buf.append(data)
            # 半双工：豆包在说话（合成中或还有音频没放完）就不上行，
            # 同时也不喂 KWS（避免扬声器里的"再见"误触发）
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
                self.drain_input(0.2)
            if self.kws is not None:
                hit = self.kws.feed(data)
                if hit and hit != WAKE_LABEL:
                    print(f"\n[结束词] {hit}")
                    self.session_done = True
                    return
            try:
                await self.client.task_request(data)
                chunks_sent += 1
                if self.debug and chunks_sent % 10 == 0:
                    print(f"[debug] 已上行 {chunks_sent} 块音频")
            except Exception as e:
                print(f"\n[上行失败] {e}")
                self.session_done = True
                return

    # ---------- 单次会话 ----------
    async def run_session(self) -> None:
        self.session_done = False
        self.doubao_speaking = False
        self.last_response_time = time.time()
        self.audio_q.queue.clear()
        self.client = RealtimeDialogClient(
            config=config.ws_connect_config, session_id=str(uuid.uuid4()))
        in_stream = self.in_stream
        self.drain_input(0.5)   # 丢掉唤醒前后积压的旧音频
        self._debug_buf.clear()
        try:
            await self.client.connect()
            if self.kws is not None:
                self.kws.set_exit_enabled(True)
            tasks = [asyncio.create_task(self.receive_loop()),
                     asyncio.create_task(self.mic_loop())]
            try:
                while self.running and not self.session_done:
                    if time.time() - self.last_response_time > IDLE_EXIT_SECONDS:
                        print("\n30 秒没有对话，自动结束本次会话。")
                        break
                    await asyncio.sleep(0.2)
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
        finally:
            if self.kws is not None:
                self.kws.set_exit_enabled(False)
            if self.debug:
                self._save_debug_audio()
            self.drain_input(0.3)   # 清掉会话结束时的尾巴，防止误唤醒

    # ---------- 回车触发模式 ----------
    async def _enter_loop(self) -> None:
        print("=" * 50)
        print("豆包英语玩伴（回车版）")
        print("  按回车 = 开始会话；会话中再按回车 = 结束")
        print("  Ctrl+C = 退出")
        print("=" * 50)
        loop = asyncio.get_running_loop()
        while self.running:
            got = await loop.run_in_executor(None, input)
            if got == "" and self.running:
                await self.run_session()

    # ---------- 语音唤醒模式 ----------
    async def _wake_loop(self) -> None:
        print("=" * 50)
        print("豆包英语玩伴（唤醒版）")
        print("  喊\"豆包豆包\"开始会话")
        print("  会话中喊\"再见/别说了/停下\"等结束，或 30 秒不说话自动结束")
        print("  Ctrl+C = 退出")
        print("=" * 50)
        self.kws = WakeSpotter(threshold=0.10)
        print("唤醒词模型已加载，正在监听...")
        try:
            while self.running:
                data = self.read_chunk()
                hit = self.kws.feed(data)
                if hit == WAKE_LABEL:
                    print("\n🔔 唤醒！")
                    self.chime()
                    await self.run_session()
                    if self.running:
                        print("已回到待机，喊\"豆包豆包\"可以再来。")
        finally:
            pass  # 输入流由 run() 统一关闭

    def _save_debug_audio(self, path: str = "debug_mic.wav") -> None:
        """把本次会话发给服务器的麦克风原声存盘，便于排查上行问题。"""
        if not self._debug_buf:
            print("[debug] 本次会话没有上行音频")
            return
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(IN_RATE)
            wf.writeframes(b"".join(self._debug_buf))
        print(f"[debug] 已保存发给服务器的原声: {path} "
              f"({len(self._debug_buf)} 块, {len(self._debug_buf) / 10:.1f}s)")

    # ---------- 主流程 ----------
    async def run(self) -> None:
        try:
            if self.enter_mode:
                await self._enter_loop()
            else:
                await self._wake_loop()
        finally:
            self.running = False
            self.session_done = True
            self.in_stream.stop_stream()
            self.in_stream.close()
            self.out_stream.stop_stream()
            self.out_stream.close()
            self.pa.terminate()
            print("已退出。")


if __name__ == "__main__":
    import wave
    asyncio.run(Companion(
        enter_mode="--enter" in sys.argv,
        debug="--debug" in sys.argv,
    ).run())
