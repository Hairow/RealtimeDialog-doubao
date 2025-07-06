import asyncio
import json
from pathlib import Path
from datetime import datetime
import argparse
from typing import List, Dict, Any

import config
from file_based_dialog import FileBasedDialog
from audio_converter import convert_audio_to_16khz


async def process_batch(input_files: List[str], output_dir: str = None) -> Dict[str, Any]:
    """
    批量处理多个音频文件

    Args:
        input_files: 输入文件列表
        output_dir: 输出目录，如果为None则在输入文件同目录下创建output文件夹

    Returns:
        Dict[str, Any]: 批处理结果
    """
    # 准备输出目录
    if output_dir is None:
        output_dir = Path(input_files[0]).parent / "output"
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(exist_ok=True)

    # 创建结果记录
    batch_result = {
        "start_time": datetime.now().isoformat(),
        "total_files": len(input_files),
        "processed": 0,
        "success": 0,
        "failed": 0,
        "results": []
    }

    print(f"开始批量处理 {len(input_files)} 个文件...")
    print(f"输出目录: {output_dir}")
    print("-" * 60)

    for idx, input_file in enumerate(input_files, 1):
        print(f"\n[{idx}/{len(input_files)}] 处理文件: {input_file}")

        file_result = {
            "input_file": input_file,
            "status": "processing",
            "error": None,
            "output_file": None,
            "converted_file": None
        }

        try:
            # 1. 检查并转换音频格式
            input_path = Path(input_file)
            temp_dir = output_dir / "temp"
            temp_dir.mkdir(exist_ok=True)

            # 检查音频格式
            need_convert = False
            try:
                import wave
                with wave.open(input_file, 'rb') as wf:
                    if wf.getframerate() != 16000 or wf.getnchannels() != 1:
                        need_convert = True
            except:
                need_convert = True

            if need_convert:
                print("  需要转换音频格式...")
                converted_file = str(temp_dir / f"{input_path.stem}_16khz.wav")
                convert_audio_to_16khz(input_file, converted_file)
                file_result["converted_file"] = converted_file
                process_file = converted_file
            else:
                process_file = input_file

            # 2. 创建对话实例并处理
            dialog = FileBasedDialog(config.ws_connect_config)
            output_file = str(output_dir / f"{input_path.stem}_reply.wav")

            # 处理音频
            result = await dialog.process_audio_file(process_file, output_file)

            # 更新结果
            file_result["status"] = result["status"]
            file_result["output_file"] = output_file if result["status"] == "success" else None
            file_result["session_id"] = result["session_id"]
            file_result["duration"] = result["duration"]
            file_result["response_chunks"] = result["response_chunks"]

            if result["status"] == "success":
                batch_result["success"] += 1
            else:
                batch_result["failed"] += 1
                file_result["error"] = result.get("error", "Unknown error")

        except Exception as e:
            print(f"  ❌ 处理失败: {e}")
            file_result["status"] = "error"
            file_result["error"] = str(e)
            batch_result["failed"] += 1

        batch_result["processed"] += 1
        batch_result["results"].append(file_result)

        # 短暂延迟，避免请求过快
        if idx < len(input_files):
            print("  等待2秒后处理下一个文件...")
            await asyncio.sleep(2)

    # 完成批处理
    batch_result["end_time"] = datetime.now().isoformat()

    # 保存批处理结果
    result_file = output_dir / f"batch_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(batch_result, f, ensure_ascii=False, indent=2)

    # 打印摘要
    print("\n" + "=" * 60)
    print("批处理完成!")
    print(f"总文件数: {batch_result['total_files']}")
    print(f"成功: {batch_result['success']}")
    print(f"失败: {batch_result['failed']}")
    print(f"结果保存到: {result_file}")

    # 清理临时文件
    temp_dir = output_dir / "temp"
    if temp_dir.exists():
        for temp_file in temp_dir.glob("*.wav"):
            temp_file.unlink()
        temp_dir.rmdir()

    return batch_result


async def process_directory(input_dir: str, pattern: str = "*.wav", output_dir: str = None) -> Dict[str, Any]:
    """
    处理目录中的所有匹配文件

    Args:
        input_dir: 输入目录
        pattern: 文件匹配模式
        output_dir: 输出目录

    Returns:
        Dict[str, Any]: 批处理结果
    """
    input_path = Path(input_dir)
    if not input_path.exists():
        raise ValueError(f"输入目录不存在: {input_dir}")

    # 查找所有匹配的文件
    input_files = list(input_path.glob(pattern))
    if not input_files:
        raise ValueError(f"在 {input_dir} 中没有找到匹配 {pattern} 的文件")

    print(f"找到 {len(input_files)} 个文件")

    # 转换为字符串路径列表
    input_files = [str(f) for f in input_files]

    return await process_batch(input_files, output_dir)


def main():
    parser = argparse.ArgumentParser(description='批量处理音频文件')
    parser.add_argument('input', nargs='+', help='输入文件或目录')
    parser.add_argument('-o', '--output', help='输出目录')
    parser.add_argument('-p', '--pattern', default='*.wav', help='文件匹配模式（用于目录处理）')

    args = parser.parse_args()

    # 判断输入是文件还是目录
    input_paths = [Path(p) for p in args.input]

    # 收集所有要处理的文件
    all_files = []

    for path in input_paths:
        if path.is_file():
            all_files.append(str(path))
        elif path.is_dir():
            # 如果是目录，查找匹配的文件
            matched_files = list(path.glob(args.pattern))
            all_files.extend([str(f) for f in matched_files])
        else:
            print(f"警告: {path} 不是有效的文件或目录，跳过")

    if not all_files:
        print("错误: 没有找到要处理的文件")
        return

    # 执行批处理
    asyncio.run(process_batch(all_files, args.output))


if __name__ == "__main__":
    main()