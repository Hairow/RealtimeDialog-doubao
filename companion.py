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
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
import wave
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
AGC_TARGET = 0.10       # 上行 AGC 目标 RMS（-20dBFS，服务端 VAD 的舒适区）
AGC_MAX = 10.0          # AGC 最大提升倍数（叠加固定增益后总能盖过阈值）
GATE_MARGIN = 1.8       # 自适应噪门倍数：超过底噪这么多倍才算"有人在说话"
READ_TIMEOUT = 3.0      # 秒：arecord 超过这么久不出数据就认为卡死，重启采集
IS_LINUX = sys.platform.startswith("linux")


def _elog(msg: str) -> None:
    """事件日志（毫秒时间戳），排查实跑与回放差异用。"""
    with open("companion_events.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')}+{int(time.time()*1000)%1000:03d} {msg}\n")


def _rms_int16(data: bytes) -> float:
    """int16 PCM 的 RMS（0-1）。必须用 float64：int16 平方会溢出成负数。"""
    x = np.frombuffer(data, dtype=np.int16).astype(np.float64) / 32768.0
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


class HighPass:
    """二阶 Butterworth 高通（120Hz），实时流式处理。

    笔记本内置麦在 +50dB 增益下会收进大量 50Hz 工频哼声和低频隆隆，
    它们占了相当一部分能量却完全不含语音，会顶住噪门、拖偏 AGC 的 RMS。
    砍掉 120Hz 以下，语音几乎无损，噪声却能少两三 dB。
    """

    def __init__(self, fc: float = 120.0, fs: int = IN_RATE) -> None:
        w0 = 2.0 * math.pi * fc / fs
        cw, sw = math.cos(w0), math.sin(w0)
        alpha = sw / (2.0 * 0.7071)
        a0 = 1.0 + alpha
        self.b = ((1.0 + cw) / 2.0 / a0, -(1.0 + cw) / a0, (1.0 + cw) / 2.0 / a0)
        self.a = (-2.0 * cw / a0, (1.0 - alpha) / a0)
        self.z1 = 0.0
        self.z2 = 0.0

    def __call__(self, data: bytes) -> bytes:
        x = np.frombuffer(data, dtype=np.int16).astype(np.float64) / 32768.0
        if not x.size:
            return data
        b0, b1, b2 = self.b
        a1, a2 = self.a
        y = np.empty_like(x)
        z1, z2 = self.z1, self.z2
        for i in range(x.size):          # 直接写转置二型（II）差分方程
            xi = x[i]
            yi = b0 * xi + z1
            z1 = b1 * xi - a1 * yi + z2
            z2 = b2 * xi - a2 * yi
            y[i] = yi
        self.z1, self.z2 = z1, z2
        return (np.clip(y, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _is_burst(data: bytes) -> bool:
    """识别采集流的整块爆音。

    这台机器的 HDA 驱动偶发把满量程垃圾塞进采集流（实测约 1/8 概率），
    特征是一整块电平爆高且频谱接近白噪。丢掉它，别让它开噪门、别写进
    上行，也别喂给唤醒模型。
    """
    x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    if not x.size:
        return False
    peak = float(np.abs(x).max())
    rms = float(np.sqrt(np.mean(x * x)))
    return peak > 0.75 and rms > 0.25


def _flatness(x: np.ndarray) -> float:
    """谱平坦度（0=纯音，1=白噪）。人声有谐波结构，噪声更平坦。"""
    n = 512
    if x.size < n:
        return 1.0
    seg = x[: n * (x.size // n)].reshape(-1, n)[-1] * np.hanning(n)
    p = np.abs(np.fft.rfft(seg)) ** 2 + 1e-12
    return float(np.exp(np.mean(np.log(p))) / np.mean(p))


class WebrtcFrontend:
    """WebRTC 音频前端（降噪 + 高通 + 语音检测），移植自 xiaoai-plus 的思路。

    xiaoai-plus 在音箱上用的是 libwebrtc_audio_processing（AEC + NS + AGC），
    其中降噪与语音检测这两块在笔记本上同样有效：NS 对平稳噪声（风扇、工频）
    的抑制比手写的电平噪门准得多，is_speech 还能给出真正的 VAD 判决。

    这里用 webrtc-noise-gain（同一套 WebRTC 算法的 Python 绑定），按 10ms
    帧处理；单独留一份 HPF 作为装不上时的回退。
    """

    def __init__(self, ns_level: int = 2) -> None:
        from webrtc_noise_gain import AudioProcessor
        self.ap = AudioProcessor(auto_gain=0, noise_suppression=ns_level)
        self.is_speech = False          # 最近一块里是否出现语音帧
        # WebRTC 的降噪按"正常通话电平"设计，对 -40dBFS 的弱信号会把语音
        # 一起判成噪声抹掉（实测：不预增益时直接降噪，原本可识别的样本变成
        # 零返回；预增益 8x 后再降噪恢复正常）。所以内部先抬电平、出结果再
        # 还原，下游拿到的音量与处理前一致。
        self.pre = 8.0

    def __call__(self, data: bytes) -> bytes:
        x = np.frombuffer(data, dtype=np.int16).astype(np.float64) * self.pre
        x = np.clip(x, -32768, 32767).astype(np.int16)
        n = len(x) // 160 * 160          # 只处理整 10ms 帧
        if n == 0:
            return data
        out = np.empty(n, dtype=np.float64)
        speech = False
        for i in range(0, n, 160):
            r = self.ap.Process10ms(x[i:i + 160].tobytes())
            out[i:i + 160] = np.frombuffer(r.audio, dtype=np.int16)
            speech = speech or bool(r.is_speech)
        self.is_speech = speech
        out /= self.pre
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


def ensure_input_volume(target: int = 100) -> None:
    """把系统输入音量拉到 target（Mac 内置麦默认偏低会导致 ASR 听不清）。"""
    if not sys.platform == "darwin":
        return   # Linux/其它平台用 amixer 配置，不在这里动
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
    rate, dur = 24000, 0.22
    n = int(rate * dur)
    t = np.arange(n)
    fade = np.minimum(1.0, np.minimum(t, n - 1 - t) / (0.02 * rate))
    wave = 0.75 * np.sin(2 * math.pi * 880 * t / rate) * fade
    return (wave * 32767).astype(np.int16).tobytes()


class AlsaCapture:
    """Linux 采集后端：用 arecord 子进程读 ALSA。

    比 PortAudio 可靠：PortAudio 只枚举 hw:* 硬件设备，看不到 asound.conf
    里的别名（如蓝牙/环回），而 arecord 认 default 与自动重采样。
    """

    def __init__(self, device: str = "default", rate: int = IN_RATE,
                 channels: int = 1) -> None:
        self.rate = rate
        self.device = device
        self.channels = channels
        self._buf = b""
        self._spawn()

    def _spawn(self) -> None:
        self._buf = b""
        self.proc = subprocess.Popen(
            ["arecord", "-q", "-D", self.device, "-f", "S16_LE",
             "-r", str(self.rate), "-c", str(self.channels), "-t", "raw",
             # 显式指定缓冲/周期（借自 xiaoai-plus 的 recorder_arecord.cpp）：
             # 不指定时驱动会按默认值给，老 HDA 上容易出现周期性抖动和突刺。
             "--buffer-size", "4800", "--period-size", "480"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def restart(self) -> None:
        """arecord 卡死/退出后重新拉起来。"""
        self.close()
        self._spawn()

    def read(self, nbytes: int, exception_on_overflow: bool = False) -> bytes:
        """读满 nbytes（管道 read 可能短读，必须循环凑齐）。

        关键：用 select 加超时。管道 read 是阻塞的，一旦 arecord 停止输出
        （这台老 HDA 上会偶发），阻塞 read 会把整个 asyncio 事件循环冻住
        —— 接收协程、看门狗全部停摆，表现为"服务端不回应"。宁可抛错让
        上层重启采集，也不能无声卡死。
        """
        while len(self._buf) < nbytes:
            ready, _, _ = select.select([self.proc.stdout], [], [],
                                        READ_TIMEOUT)
            if not ready:
                raise OSError(f"arecord {READ_TIMEOUT:.0f}s 没有输出音频（卡死）")
            chunk = self.proc.stdout.read(nbytes - len(self._buf))
            if not chunk:
                raise OSError("arecord 已退出（设备被占用或不可用）")
            self._buf += chunk
        out, self._buf = self._buf[:nbytes], self._buf[nbytes:]
        return out

    def flush_pending(self) -> None:
        """丢掉管道里所有积压音频（半双工恢复时用，避免听到十几秒前的话）。"""
        self._buf = b""
        while True:
            ready, _, _ = select.select([self.proc.stdout], [], [], 0)
            if not ready:
                break
            if not self.proc.stdout.read(65536):
                break

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            pass


class AlsaPlayback:
    """Linux 播放后端：把 PCM 写进 aplay 子进程。

    device 可填 asound.conf 里的别名（如 bt = 蓝牙音箱），aplay 会自动
    把 24k 单声道转成蓝牙需要的 48k 立体声。
    """

    def __init__(self, device: str = "bt", rate: int = OUT_RATE,
                 channels: int = 1, buffer_us: int = 200000) -> None:
        self.proc = subprocess.Popen(
            ["aplay", "-q", "-D", device, "-f", "S16_LE",
             "-r", str(rate), "-c", str(channels), "-t", "raw",
             "-B", str(buffer_us)],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._dead = False

    def write(self, data: bytes) -> None:
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            # 播放进程已退出（设备被占用或断开），丢弃这块音频；
            # 但只提示一次，否则每块都刷一行把日志淹了
            if not self._dead:
                self._dead = True
                print("[音频] 播放进程已退出（设备被占用或断开），"
                      "声音不会出来", flush=True)

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except Exception:
            pass


class Companion:
    def __init__(self, enter_mode: bool = False, debug: bool = False,
                 synthetic: bool = False, alsa: bool = False,
                 device_in: str = "default", device_out: str = "bt",
                 gain: float = UPLINK_GAIN, gate: float = GATE_RMS,
                 threshold: float = 0.10, ns_level: int = 2) -> None:
        self.enter_mode = enter_mode
        self.synthetic = synthetic      # --synthetic：不发麦克风，发合成测试音频
        self.alsa = alsa                # --alsa：Linux 上用 arecord/aplay 子进程
        self.device_in = device_in
        self.device_out = device_out
        self.gain = gain                # 上行固定增益（不同麦克风灵敏度差很多）
        self.gate = gate                # 上行噪门阈值
        self.threshold = threshold      # 唤醒词判定阈值（麦克风弱就调低）
        self.agc_gain = 1.0             # 上行 AGC 当前增益（跨块平滑）
        self.agc_max = AGC_MAX          # AGC 最大提升倍数
        self.noise_floor = 0.010        # 跟踪中的底噪 RMS（自适应噪门用）
        self._synth_chunks: list | None = None
        self._synth_idx = 0
        self.session_id = str(uuid.uuid4())
        self.client = RealtimeDialogClient(config=config.ws_connect_config,
                                           session_id=self.session_id)
        self.pa = None
        if alsa:
            self.out_stream = AlsaPlayback(device_out, rate=OUT_RATE)
            print(f"[音频] 输出设备: {device_out!r}")
        else:
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
        self._dbg_fh = None              # 实时写 debug_sent.raw 的文件句柄
        self.raw_buf_ok = True           # 原始音频留档
        self._raw_buf: list[bytes] = []  # 处理前的原始音频（高通/增益之前）
        self.bursts = 0                 # 已丢弃的爆音块数
        self.hpf = HighPass(120.0, IN_RATE)  # 去掉 120Hz 以下工频与隆隆声
        self.frontend = None            # WebRTC 降噪前端（装得上就用）
        self.ns_level = ns_level
        if ns_level > 0:
            try:
                self.frontend = WebrtcFrontend(ns_level)
                print(f"[音频] 已启用 WebRTC 降噪（等级 {ns_level}）与语音检测")
                # 降噪会把底噪压到 0.001 量级，语音也同步变小，原来的
                # 噪门阈值（0.015）会把正常说话一起切掉，这里跟着下移。
                if self.gate > 0.006:
                    self.gate = 0.005
                    print(f"[音频] 噪门阈值随降噪调整为 {self.gate}")
            except Exception as e:
                print(f"[音频] WebRTC 降噪不可用（{e}），退回高通滤波")
        # 全程共用一个输入流（避免待机/会话各开一个的冲突）
        self.in_stream, self.in_rate = self.open_input()
        self.player = threading.Thread(target=self._play_loop, daemon=True)
        self.player.start()
        signal.signal(signal.SIGINT, self._sigint)

    # ---------- 音频流 ----------
    def open_input(self):
        """优先 16k 直开；设备不支持则按默认采样率开、读取时重采样。"""
        if self.alsa:
            print(f"[音频] 采集设备: {self.device_in!r} @ {IN_RATE}Hz")
            return AlsaCapture(self.device_in, rate=IN_RATE), IN_RATE
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
        """读 100ms 音频：重采样到 16k → 丢爆音 → 高通。"""
        in_stream = in_stream or self.in_stream
        rate = rate or self.in_rate
        raw = in_stream.read(int(CHUNK * rate / IN_RATE),
                             exception_on_overflow=False)
        if rate != IN_RATE:
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
            target = len(x) * IN_RATE // rate
            idx = np.linspace(0, len(x) - 1, target)
            raw = np.interp(idx, np.arange(len(x)), x).astype(np.int16).tobytes()
        if _is_burst(raw):               # 驱动偶发塞进来的满量程垃圾
            self.bursts += 1
            raw = b"\x00" * len(raw)
        if self.debug:
            self._raw_buf.append(raw)    # 存处理前的原始音频，供离线验尸
        if self.frontend is not None:    # WebRTC：降噪 + 高通 + VAD
            return self.frontend(raw)
        return self.hpf(raw)


    def _recover_capture(self, err: OSError) -> None:
        """采集卡死/退出时重启 arecord。"""
        print(f"\n[采集] {err}，重启 arecord", flush=True)
        _elog(f"采集异常: {err}")
        restart = getattr(self.in_stream, "restart", None)
        if restart is None:
            return
        try:
            restart()
        except Exception as e:
            _elog(f"重启采集失败: {e}")

    def _safe_read(self) -> bytes:
        """读到一块已加固定的上行音频；卡死时返回静音，不让主循环崩。

        必须在线程里调用：arecord 的管道读是阻塞的，直接跑会把 asyncio
        事件循环冻住（实测导致接收协程停摆、看门狗失效）。
        """
        try:
            return self._boost(self.read_chunk())
        except OSError as e:
            self._recover_capture(e)
            return b"\x00" * CHUNK

    def _safe_read_raw(self) -> bytes:
        """唤醒循环用：只做重采样/高通，不加固定增益（KWS 自带 AGC）。"""
        try:
            return self.read_chunk()
        except OSError as e:
            self._recover_capture(e)
            return b"\x00" * CHUNK

    def drop_pending(self) -> None:
        """丢掉输入流积压（半双工恢复时管道可能攒了十几秒旧音频）。"""
        flush = getattr(self.in_stream, "flush_pending", None)
        if flush is None:
            self.drain_input(0.2)
            return
        try:
            flush()
        except OSError as e:
            self._recover_capture(e)

    def drain_input(self, seconds: float = 0.5) -> None:
        """丢掉输入流里积压的旧音频（会话开始前/结束后清尾巴）。"""
        n = int(seconds * 10)  # 每 chunk = 100ms
        for _ in range(n):
            try:
                self.read_chunk()
            except OSError as e:
                self._recover_capture(e)
                return

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
        # 走播放队列，避免主线程与播放线程同时写同一个输出流
        self.audio_q.put(_chime_bytes())

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
        diag_at = time.monotonic()
        try:
            while self.running and not self.session_done:
                try:
                    resp = await asyncio.wait_for(
                        self.client.receive_server_response(),
                        timeout=1.0)
                except asyncio.TimeoutError:
                    # 诊断：recv 一直超时时，看 websocket 内部队列里到底有没有帧
                    if time.monotonic() - diag_at > 5:
                        diag_at = time.monotonic()
                        q = getattr(getattr(self.client.ws, "messages", None),
                                    "qsize", None)
                        try:
                            qs = q() if q else "?"
                        except Exception:
                            qs = "err"
                        _elog(f"diag recv超时 n_recv={n_recv} 队列={qs} "
                              f"状态={getattr(self.client.ws, 'state', '?')} "
                              f"open={getattr(self.client.ws, 'open', '?')}")
                    continue
                except Exception as e:
                    print(f"\n接收中断: {e}")
                    _elog(f"recv_loop 异常退出: {e}")
                    break
                n_recv += 1
                if resp == {}:
                    n_empty += 1
                    if n_empty <= 5:
                        _elog(f"空消息#{n_empty} type={type(resp)}")
                    continue
                # 只有真实事件才刷新看门狗；空消息/心跳不刷新，
                # 否则一次会话能被心跳无限续命，一直发静音烧额度
                self.last_response_time = time.time()
                event = resp.get("event")
                mt = resp.get("message_type")
                if n_recv <= 3:
                    print(f"[recv] #{n_recv} ev={event} mt={mt}", flush=True)
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

    def _boost(self, data: bytes) -> bytes:
        """上行固定增益 + 饱和限幅（唤醒已有动态 AGC，上行用保守增益）。"""
        if self.gain == 1.0:
            return data
        x = (np.frombuffer(data, dtype=np.int16).astype(np.float64) * self.gain)
        return np.clip(np.rint(x), -32768, 32767).astype(np.int16).tobytes()

    def _agc(self, data: bytes, enabled: bool = True) -> tuple[bytes, float]:
        """上行 AGC：把语音拉到距满刻度约 -20dBFS 的目标电平。

        笔记本内置麦的问题是动态范围——喊唤醒词有 -30dBFS，正常说话只有
        -40dBFS，服务端 VAD 只认前一种。固定增益要么盖不住小声、要么把
        喊声削平，所以这里按块做 RMS 归一化，且只对"判定为说话"的块生效：
        静音段若也放大，底噪会被抬到 -20dBFS，服务端就无法判断句首句尾了。
        返回（处理后的 PCM，处理前的 RMS）。
        """
        x = np.frombuffer(data, dtype=np.int16).astype(np.float64) / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        if not enabled:                      # 静音段：不放大，增益缓慢回落
            self.agc_gain = max(1.0, self.agc_gain * 0.97)
            return data, rms
        target = float(np.clip(AGC_TARGET / max(rms, 1e-6), 0.5, self.agc_max))
        # 慢起快落：说话开头不至于突变，收尾不至于拖长
        self.agc_gain = 0.6 * self.agc_gain + 0.4 * target
        y = np.clip(x * self.agc_gain, -1.0, 1.0)
        return (y * 32767).astype(np.int16).tobytes(), rms

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
                raw = await asyncio.to_thread(self._safe_read)
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
                # 丢掉闭麦期间积压的旧音频，否则会听到十几秒前的声音
                self.drop_pending()
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
            rms = _rms_int16(raw)
            # 自适应噪门：底噪会随风扇/环境漂移，写死阈值不是好一点就是漏一点。
            # 低于底噪快速下跟；落在底噪带内缓慢上爬；明显是语音则按住不动。
            if rms < self.noise_floor:
                self.noise_floor = 0.7 * self.noise_floor + 0.3 * rms
            elif rms < self.noise_floor * GATE_MARGIN:
                self.noise_floor = 0.98 * self.noise_floor + 0.02 * rms
            thr = max(self.gate, self.noise_floor * GATE_MARGIN)
            if rms > thr:
                gate_keep = GATE_HANGOVER
            elif gate_keep > 0:
                gate_keep -= 1
            norm, _ = self._agc(raw, enabled=(gate_keep > 0))
            data = norm if gate_keep > 0 else b"\x00" * len(raw)
            if self.debug:
                self._debug_buf.append(data)
                if self._dbg_fh is not None:   # 实时落盘，会话中途也能验尸
                    try:
                        self._dbg_fh.write(data)
                        self._dbg_fh.flush()
                    except (OSError, ValueError):
                        pass
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
                    _elog(f"sent#{chunks_sent} rms={rms:.3f} thr={thr:.3f} "
                          f"gate={gate_keep} agc={self.agc_gain:.1f} "
                          f"nf={self.noise_floor:.4f} muted={muted} "
                          f"alive={self.session_done}")
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
        self.client = RealtimeDialogClient(
            config=config.ws_connect_config, session_id=str(uuid.uuid4()))
        in_stream = self.in_stream
        self.drain_input(0.5)   # 丢掉唤醒前后积压的旧音频
        self._debug_buf.clear()
        self._raw_buf.clear()
        self.bursts = 0
        if self.debug:
            try:
                self._dbg_fh = open("debug_sent.raw", "wb")
            except OSError:
                self._dbg_fh = None
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
                if self._dbg_fh is not None:
                    try:
                        self._dbg_fh.close()
                    except OSError:
                        pass
                    self._dbg_fh = None
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
        self.kws = WakeSpotter(threshold=self.threshold)
        self.chime()   # 启动自检音：听不到说明播放链路有问题
        print(f"唤醒词模型已加载，正在监听...（阈值 {self.threshold}）", flush=True)
        peak = 0
        sumsq = 0.0
        nsamp = 0
        peak_at = time.time()
        try:
            while self.running:
                data = await asyncio.to_thread(self._safe_read_raw)
                # 麦克风电平自检：每秒最多打一行，静音时不打，用来区分
                # "根本没拾到声音" 和 "拾到了但唤醒词没命中"
                x = np.frombuffer(data, dtype=np.int16)
                if len(x):
                    f = x.astype(np.float64) / 32768.0
                    peak = max(peak, int(np.abs(x.astype(np.int32)).max()))
                    sumsq += float(np.dot(f, f))
                    nsamp += len(f)
                if time.time() - peak_at >= 1.0:
                    if nsamp and peak > 150:
                        rms = math.sqrt(sumsq / nsamp)
                        bar = "#" * min(30, peak // 400)
                        print(f"[mic {time.strftime('%H:%M:%S')}] 峰值 {peak:5d} "
                              f"rms {rms:.4f} {bar}", flush=True)
                    peak = 0
                    sumsq = 0.0
                    nsamp = 0
                    peak_at = time.time()
                hit = self.kws.feed(data)
                if hit == WAKE_LABEL:
                    print("\n🔔 唤醒！")
                    # 先清掉上一轮残留，再放提示音；顺序反了会把"叮"清掉
                    self.audio_q.queue.clear()
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
        if self._raw_buf:
            with wave.open("debug_raw.wav", "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(IN_RATE)
                wf.writeframes(b"".join(self._raw_buf))
            print(f"[debug] 已保存处理前原始音频: debug_raw.wav "
                  f"({len(self._raw_buf)} 块) 丢弃爆音 {self.bursts} 块")

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
            if self.pa is not None:
                self.pa.terminate()
            print("已退出。")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="豆包英语玩伴")
    ap.add_argument("--enter", action="store_true", help="回车触发模式（默认语音唤醒）")
    ap.add_argument("--debug", action="store_true", help="保存上行原声，便于排查")
    ap.add_argument("--synthetic", action="store_true", help="合成音频对照模式")
    ap.add_argument("--alsa", action="store_true", default=IS_LINUX,
                    help="用 arecord/aplay 子进程（Linux 默认开，Mac 用 pyaudio）")
    ap.add_argument("--no-alsa", dest="alsa", action="store_false", help="强制用 pyaudio")
    ap.add_argument("--device-in", default="default", help="采集设备（arecord -D 的名字）")
    ap.add_argument("--device-out", default="bt", help="播放设备（aplay -D 的名字）")
    ap.add_argument("--gain", type=float, default=UPLINK_GAIN, help="上行固定增益")
    ap.add_argument("--gate", type=float, default=GATE_RMS, help="噪门阈值（0-1 的 RMS）")
    ap.add_argument("--threshold", type=float, default=0.10,
                    help="唤醒词阈值（0.05~0.25，麦克风弱就调低）")
    ap.add_argument("--ns", type=int, default=2,
                    help="WebRTC 降噪等级 0~3（0=关闭，退回高通滤波）")
    a = ap.parse_args()
    asyncio.run(Companion(
        enter_mode=a.enter, debug=a.debug, synthetic=a.synthetic,
        alsa=a.alsa, device_in=a.device_in, device_out=a.device_out,
        gain=a.gain, gate=a.gate, threshold=a.threshold, ns_level=a.ns,
    ).run())
