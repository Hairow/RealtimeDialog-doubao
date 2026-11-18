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
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pyaudio

import config
from realtime_dialog_client import RealtimeDialogClient
from wakeup import SAMPLE_RATE as IN_RATE, WAKE_LABEL, WakeSpotter

OUT_RATE = config.output_audio_config["sample_rate"]   # 24000
CHUNK = config.input_audio_config["chunk"]             # 3200 字节 = 100ms
IDLE_EXIT_SECONDS = 30  # 服务端 30 秒无响应自动结束会话
UPLINK_GAIN = 4.0       # 上行固定增益：Mac 内置麦拾音弱，实测语音 RMS 只有正常 1/5
GATE_RMS = 0.10         # 上行噪门阈值：低于它的块发静音（增益后底噪~0.06、语音~0.2+）
GATE_HANGOVER = 10      # 说话结束后保持放行的块数（10 块 = 1 秒，防止切断词尾）


def _elog(msg: str) -> None:
    """事件日志（毫秒时间戳），排查实跑与回放差异用。"""
    with open("companion_events.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')}+{int(time.time()*1000)%1000:03d} {msg}\n")


def ensure_input_volume(target: int = 100) -> None:
    """把系统输入音量拉到 target（Mac 内置麦默认偏低会导致 ASR 听不清）。"""
    try:
        cur = int(subprocess.run(
            ["osascript", "-e", "input volume of (get volume settings)"],
            capture_output=True, text=True, timeout=5).stdout.strip() or "0")
        if cur < target:
            subprocess.run(["osascript", "-e", f"set volume input volume {target}"],
                           capture_output=True, timeout=5)
            print(f"[提示] 系统输入音量 {cur}% → {target}%")
        else:
            print(f"[提示] 系统输入音量 {cur}%")
    except Exception as e:
        print(f"[提示] 无法调整输入音量: {e}")


def _chime_bytes() -> bytes:
    """一声短促的"叮"，提示唤醒成功。"""
    rate, dur = 24000, 0.18
    n = int(rate * dur)
    t = np.arange(n)
    fade = np.minimum(1.0, np.minimum(t, n - 1 - t) / (0.02 * rate))
    wave = 0.5 * np.sin(2 * math.pi * 880 * t / rate) * fade
    return (wave * 32767).astype(np.int16).tobytes()


class Companion:
    def __init__(self, enter_mode: bool = False, debug: bool = False,
                 synthetic: bool = False) -> None:
        self.enter_mode = enter_mode
        self.synthetic = synthetic      # --synthetic：不发麦克风，发合成测试音频
        self._synth_chunks: list | None = None
        self._synth_idx = 0
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
        _elog("recv_loop 启动")
        n_recv = 0
        n_empty = 0
        suppressed: dict = {}
        try:
            while self.running and not self.session_done:
                try:
                    resp = await asyncio.wait_for(
                        self.client.receive_server_response(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"\n接收中断: {e}")
                    _elog(f"recv_loop 异常退出: {e}")
                    break
                n_recv += 1
                self.last_response_time = time.time()
                if resp == {}:
                    n_empty += 1
                    if n_empty <= 5:
                        _elog(f"空消息#{n_empty} type={type(resp)}")
                    continue
                event = resp.get("event")
                mt = resp.get("message_type")
                if mt == "SERVER_ACK" and isinstance(resp.get("payload_msg"), bytes):
                    _elog(f"TTS {len(resp['payload_msg'])}B (累计recv={n_recv})")
                elif event in (450, 451, 50, 150, 154, 52):
                    # 被抑制的事件也计数，每 20 条汇总一次
                    key = f"ev{event}"
                    suppressed[key] = suppressed.get(key, 0) + 1
                    if sum(suppressed.values()) % 20 == 0:
                        _elog(f"recv统计 total={n_recv} empty={n_empty} "
                              f"suppressed={suppressed}")
                    if event == 451:
                        for r in (resp.get("payload_msg") or {}).get("results", []):
                            if r.get("is_interim") and r.get("text"):
                                if sum(suppressed.values()) % 20 == 1:
                                    _elog(f"interim: {r['text'][:40]}")
                else:
                    _elog(f"event={event} mt={mt} payload={str(resp.get('payload_msg'))[:100]}")
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
                except Exception as e:
                    # 单条消息解析失败只跳过，不让接收循环静默死亡
                    print(f"\n[解析异常 event={resp.get('event')}] {e}")
        finally:
            _elog(f"recv_loop 退出: total={n_recv} empty={n_empty} "
                  f"suppressed={suppressed} session_done={self.session_done}")

    @staticmethod
    def _boost(data: bytes) -> bytes:
        """上行固定增益 + 饱和限幅（唤醒已有动态 AGC，上行用保守增益）。"""
        x = (np.frombuffer(data, dtype=np.int16).astype(np.float64) * UPLINK_GAIN)
        return np.clip(np.rint(x), -32768, 32767).astype(np.int16).tobytes()

    # ---------- 会话中的麦克风（半双工 + 噪门 + 结束词监听） ----------
    def _prepare_synthetic(self) -> None:
        """生成合成测试音频（say + afconvert），切成 3200B 块备用。"""
        import wave as _wave
        aiff, wav = "/tmp/doubao_synth.aiff", "/tmp/doubao_synth.wav"
        subprocess.run(["say", "-v", "Samantha",
                        "Hello Doubao, please tell me a short story about a dragon.",
                        "-o", aiff], check=True, capture_output=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000",
                        "-c", "1", aiff, wav], check=True, capture_output=True)
        with _wave.open(wav, "rb") as w:
            audio = w.readframes(w.getnframes())
        self._synth_chunks = [audio[i:i + CHUNK]
                              for i in range(0, len(audio), CHUNK)]
        print(f"[synthetic] 已生成 {len(self._synth_chunks)} 块测试音频"
              f"（{len(audio)/32000:.1f}s）")

    async def mic_loop(self) -> None:
        if self.synthetic and self._synth_chunks is None:
            self._prepare_synthetic()
        print("麦克风已开，直接说话即可。豆包说话时会自动闭麦。"
              if not self.synthetic else "[synthetic] 会话音频来自合成测试文件")
        muted = False
        chunks_sent = 0
        gate_keep = 0   # 噪门放行剩余块数
        self._last_send = 0.0
        while self.running and not self.session_done:
            if self.synthetic:
                if self._synth_idx < len(self._synth_chunks):
                    raw = self._synth_chunks[self._synth_idx]
                    self._synth_idx += 1
                else:
                    raw = b"\x00" * CHUNK   # 发完后补静音
                await asyncio.sleep(0.1)
            else:
                raw = self._boost(self.read_chunk())
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
            # 结束词监听（只在非闭麦时）
            if self.kws is not None:
                hit = self.kws.feed(raw)
                if hit and hit != WAKE_LABEL:
                    print(f"\n[结束词] {hit}")
                    self.session_done = True
                    return
            # 噪门：增益后底噪仍会顶住服务端 VAD，低于阈值发静音，
            # 让服务器能判断"说完了"。注意必须用 float64 计算 RMS，
            # int16 平方会溢出成负数 -> NaN -> 噪门永远不开（踩过的坑）
            rms = float(np.sqrt(np.mean(
                np.frombuffer(raw, np.int16).astype(np.float64) ** 2)) / 32768.0)
            if rms > GATE_RMS:
                gate_keep = GATE_HANGOVER
            elif gate_keep > 0:
                gate_keep -= 1
            data = raw if gate_keep > 0 else b"\x00" * len(raw)
            if self.debug:
                self._debug_buf.append(data)
            now = time.monotonic()
            if self._last_send > 0:
                interval = (now - self._last_send) * 1000
                if interval > 300:  # 超过 3 个块周期，说明发送节奏被卡住
                    _elog(f"!! 发送间隔 {interval:.0f}ms (chunk#{chunks_sent})")
            self._last_send = now
            try:
                await self.client.task_request(data)
                chunks_sent += 1
                if chunks_sent % 10 == 0:
                    _elog(f"sent#{chunks_sent} rms={rms:.3f} gate={gate_keep} "
                          f"muted={muted} alive={self.session_done}")
                if self.debug and chunks_sent % 10 == 0:
                    print(f"[debug] 已上行 {chunks_sent} 块音频")
            except Exception as e:
                _elog(f"上行失败: {e}")
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
            t_start = time.monotonic()
            await self.client.connect()
            _elog(f"连接+会话建立 {time.monotonic()-t_start:.2f}s")
            if self.kws is not None:
                self.kws.set_exit_enabled(True)
            tasks = [asyncio.create_task(self.receive_loop()),
                     asyncio.create_task(self.mic_loop())]
            try:
                while self.running and not self.session_done:
                    if time.time() - self.last_response_time > IDLE_EXIT_SECONDS:
                        print("\n30 秒没有对话，自动结束本次会话。")
                        _elog("30s 看门狗结束会话")
                        break
                    await asyncio.sleep(0.2)
            finally:
                self.session_done = True
                try:
                    await self.client.finish_session()
                except Exception as e:
                    _elog(f"finish_session 异常: {e}")
                # 等收尾事件/播放完
                deadline = time.time() + 5
                while time.time() < deadline and (not self.audio_q.empty()):
                    await asyncio.sleep(0.1)
                await asyncio.sleep(0.8)  # 让最后一小段播完
                for t in tasks:
                    t.cancel()
                # 等两个任务真正退出，避免收尾时 recv 竞态
                await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await self.client.finish_connection()
                    await self.client.close()
                except Exception as e:
                    _elog(f"收尾连接异常: {e}")
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
        ensure_input_volume()
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
            if self.enter_mode or self.synthetic:
                if self.synthetic:
                    print("[synthetic] 合成音频对照模式：按回车开始会话")
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
        synthetic="--synthetic" in sys.argv,
    ).run())
