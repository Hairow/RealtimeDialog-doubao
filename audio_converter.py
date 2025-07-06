import wave
import struct
import argparse
from pathlib import Path


def convert_audio_to_16khz(input_file: str, output_file: str = None) -> str:
    """
    将音频文件转换为16kHz, 16-bit, 单声道格式

    Args:
        input_file: 输入音频文件路径
        output_file: 输出音频文件路径，如果为None则自动生成

    Returns:
        str: 输出文件路径
    """
    if output_file is None:
        input_path = Path(input_file)
        output_file = str(input_path.parent / f"{input_path.stem}_16khz.wav")

    print(f"读取输入文件: {input_file}")

    with wave.open(input_file, 'rb') as wf_in:
        # 获取原始音频参数
        channels = wf_in.getnchannels()
        sample_width = wf_in.getsampwidth()
        framerate = wf_in.getframerate()
        n_frames = wf_in.getnframes()

        print(f"原始格式: {channels}声道, {framerate}Hz, {sample_width * 8}bit")

        # 读取所有数据
        audio_data = wf_in.readframes(n_frames)

        # 如果是立体声，转换为单声道
        if channels == 2:
            print("转换立体声为单声道...")
            # 解包数据
            if sample_width == 2:
                samples = struct.unpack(f'{n_frames * 2}h', audio_data)
                # 取左右声道的平均值
                mono_samples = [(samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples), 2)]
            else:
                raise ValueError(f"不支持的采样位宽: {sample_width * 8}bit")
        else:
            if sample_width == 2:
                mono_samples = struct.unpack(f'{n_frames}h', audio_data)
            else:
                raise ValueError(f"不支持的采样位宽: {sample_width * 8}bit")

        # 重采样到16kHz
        if framerate != 16000:
            print(f"重采样: {framerate}Hz -> 16000Hz...")
            # 简单的线性插值重采样
            ratio = 16000 / framerate
            new_length = int(len(mono_samples) * ratio)
            new_samples = []

            for i in range(new_length):
                # 计算原始样本的位置
                src_pos = i / ratio
                src_idx = int(src_pos)

                if src_idx >= len(mono_samples) - 1:
                    new_samples.append(mono_samples[-1])
                else:
                    # 线性插值
                    frac = src_pos - src_idx
                    sample = int(mono_samples[src_idx] * (1 - frac) + mono_samples[src_idx + 1] * frac)
                    new_samples.append(sample)

            mono_samples = new_samples

    # 保存为16kHz, 16-bit, 单声道
    print(f"保存输出文件: {output_file}")
    with wave.open(output_file, 'wb') as wf_out:
        wf_out.setnchannels(1)
        wf_out.setsampwidth(2)
        wf_out.setframerate(16000)
        wf_out.writeframes(struct.pack(f'{len(mono_samples)}h', *mono_samples))

    print(f"转换完成! 输出格式: 1声道, 16000Hz, 16bit")
    print(f"音频时长: {len(mono_samples) / 16000:.2f}秒")

    return output_file


def record_audio(output_file: str, duration: float = 5.0) -> str:
    """
    录制音频（需要pyaudio）

    Args:
        output_file: 输出文件路径
        duration: 录制时长（秒）

    Returns:
        str: 输出文件路径
    """
    try:
        import pyaudio
    except ImportError:
        print("错误: 需要安装pyaudio才能使用录音功能")
        print("请运行: pip install pyaudio")
        return None

    print(f"准备录制 {duration} 秒音频...")
    print("按Enter开始录音...")
    input()

    # 音频参数
    CHUNK = 1024
    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    RATE = 16000

    p = pyaudio.PyAudio()

    stream = p.open(format=FORMAT,
                    channels=CHANNELS,
                    rate=RATE,
                    input=True,
                    frames_per_buffer=CHUNK)

    print("录音中...")
    frames = []

    for i in range(0, int(RATE / CHUNK * duration)):
        data = stream.read(CHUNK)
        frames.append(data)

        # 显示进度
        progress = (i + 1) / (RATE / CHUNK * duration) * 100
        print(f"\r进度: {progress:.1f}%", end='')

    print("\n录音完成!")

    stream.stop_stream()
    stream.close()
    p.terminate()

    # 保存录音
    with wave.open(output_file, 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(p.get_sample_size(FORMAT))
        wf.setframerate(RATE)
        wf.writeframes(b''.join(frames))

    print(f"音频已保存到: {output_file}")
    return output_file


def main():
    parser = argparse.ArgumentParser(description='音频格式转换和录音工具')
    subparsers = parser.add_subparsers(dest='command', help='命令')

    # 转换命令
    convert_parser = subparsers.add_parser('convert', help='转换音频格式到16kHz')
    convert_parser.add_argument('input', help='输入音频文件')
    convert_parser.add_argument('-o', '--output', help='输出文件路径（可选）')

    # 录音命令
    record_parser = subparsers.add_parser('record', help='录制音频')
    record_parser.add_argument('-o', '--output', default='recorded.wav', help='输出文件路径')
    record_parser.add_argument('-d', '--duration', type=float, default=5.0, help='录制时长（秒）')

    args = parser.parse_args()

    if args.command == 'convert':
        convert_audio_to_16khz(args.input, args.output)
    elif args.command == 'record':
        record_audio(args.output, args.duration)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()