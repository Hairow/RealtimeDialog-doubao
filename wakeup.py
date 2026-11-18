"""阶段3：sherpa-onnx 本地关键词唤醒

与音箱项目（xiaoai-plus）对齐的部分：
- 模型：sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20（chunk-8，
  encoder/joiner 用 int8、decoder 用浮点，见 xiaoai-doubao/THIRD_PARTY.md）
- 关键词表：keywords/keywords_doubao_repeat_end.txt 原样照搬
- 阈值：唤醒词 0.10（关键词行内 # 值为兜底下限）
- AGC：audio/kws_preprocess.cpp 的 KwsAgc 逐行翻译（10ms 帧、
  平滑 0.9/0.1、目标 -25dBFS、增益 1.0~15.85、饱和限幅）
"""
import math
import pathlib

import numpy as np
import sherpa_onnx

MODEL_DIR = pathlib.Path(__file__).parent / "models" / "kws"
SAMPLE_RATE = 16000
WAKE_LABEL = "豆包豆包"


class KwsAgc:
    """帧级因果 RMS 自动增益，参数与 xiaoai-plus 完全一致。"""

    def __init__(self) -> None:
        self.envelope = 0.0
        self.gain = 1.0

    def process(self, x: np.ndarray) -> np.ndarray:
        """输入 int16 numpy 数组，返回增益后的 int16 数组。"""
        out = x.astype(np.float64)
        n_total = len(out)
        for off in range(0, n_total, 160):  # 160 采样 = 10ms @16k
            frame = out[off:off + 160]
            energy = float(np.mean((frame / 32768.0) ** 2))
            self.envelope = 0.9 * self.envelope + 0.1 * energy
            desired = float(np.clip(0.0562 / max(math.sqrt(self.envelope), 1e-6),
                                    1.0, 15.85))
            self.gain = 0.9 * self.gain + 0.1 * desired
            frame *= self.gain
        return np.clip(np.rint(out), -32768, 32767).astype(np.int16)


class WakeSpotter:
    """关键词识别器。待机只听唤醒词；会话中可临时加上结束词。"""

    def __init__(self, threshold: float = 0.10, num_threads: int = 1) -> None:
        lines = [l.strip() for l in (MODEL_DIR / "keywords.txt").read_text(
            encoding="utf-8").splitlines()]
        lines = [l for l in lines if l and not l.startswith(("#", ";"))]
        # 行格式：tokens #score @label
        self.wake_lines = [l for l in lines if l.split("@", 1)[1] == WAKE_LABEL]
        self.exit_lines = [l for l in lines if l.split("@", 1)[1] != WAKE_LABEL]
        if not self.wake_lines:
            raise RuntimeError("keywords.txt 中没有唤醒词")
        self.agc = KwsAgc()
        # sherpa-onnx 2.0.1：KeywordSpotter 直接接收参数（无 Config 类）
        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(MODEL_DIR / "tokens.txt"),
            encoder=str(MODEL_DIR / "encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx"),
            decoder=str(MODEL_DIR / "decoder-epoch-13-avg-2-chunk-8-left-64.onnx"),
            joiner=str(MODEL_DIR / "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx"),
            keywords_file=str(self._wake_only_file()),
            num_threads=num_threads,
            provider="cpu",
            keywords_threshold=threshold,
            keywords_score=3.0,
            num_trailing_blanks=0,
            max_active_paths=8,
        )
        self.stream = self.spotter.create_stream()

    @staticmethod
    def _wake_only_file() -> pathlib.Path:
        p = MODEL_DIR / "keywords_wake.txt"
        if not p.exists():
            src = (MODEL_DIR / "keywords.txt").read_text(encoding="utf-8")
            wake = [l for l in src.splitlines()
                    if l.strip() and l.split("@", 1)[1] == WAKE_LABEL]
            p.write_text("\n".join(wake) + "\n", encoding="utf-8")
        return p

    def set_exit_enabled(self, enabled: bool) -> None:
        """会话开始/结束时切换监听词表（参照 kws_zipformer.cpp）。"""
        lines = self.wake_lines + self.exit_lines if enabled else None
        if lines:
            self.stream = self.spotter.create_stream("/".join(lines))
        else:
            self.stream = self.spotter.create_stream()

    def feed(self, pcm_bytes: bytes) -> str:
        """喂入 int16 PCM（16000Hz 单声道），命中则返回关键词文本。"""
        x = np.frombuffer(pcm_bytes, dtype=np.int16)
        x = self.agc.process(x)
        self.stream.accept_waveform(SAMPLE_RATE, (x / 32768.0).tolist())
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
            result = self.spotter.get_result(self.stream)
            if result:
                self.spotter.reset_stream(self.stream)
                return result
        return ""
