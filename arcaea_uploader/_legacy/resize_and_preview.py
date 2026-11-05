from PIL import Image
import os
import sys
import json
import subprocess
import argparse
import tempfile
from pathlib import Path

def process_images(root_dir):
    """
    递归处理目标目录下所有符合条件的图片和重命名
    :param root_dir: 要扫描的根目录路径
    """
    # 定义目标文件名规则
    resize_targets = ('1080_base_256.jpg', 'base_256.jpg', '3_256.jpg')
    rename_targets = ('base.jpg')
    target_size = (384, 384)

    for foldername, subfolders, filenames in os.walk(root_dir):
        # 先处理重命名操作
        for filename in filenames:
            if filename in rename_targets:
                original_path = os.path.join(foldername, filename)
                new_name = '1080_base.jpg'
                new_path = os.path.join(foldername, new_name)
                
                try:
                    os.rename(original_path, new_path)
                    print(f"已重命名: {original_path} → {new_name}")
                except Exception as e:
                    print(f"重命名 {original_path} 失败: {str(e)}")

        # 处理图片调整尺寸和重命名
        for filename in filenames:
            if filename in resize_targets:
                file_path = os.path.join(foldername, filename)
                
                try:
                    with Image.open(file_path) as img:
                        # 调整尺寸，新增尺寸判断
                        if img.size == target_size:
                            print(f"已跳过: {file_path}（尺寸已符合要求）")
                            continue
                        resized_img = img.resize(target_size, Image.LANCZOS)
                        
                        # 处理透明通道问题
                        if resized_img.mode in ('RGBA', 'LA'):
                            resized_img = resized_img.convert('RGB')
                        
                        # 覆盖保存原文件
                        resized_img.save(file_path, quality=90, optimize=True)
                        print(f"已调整尺寸: {file_path} → {target_size[0]}x{target_size[1]}")

                        # 对base_256.jpg进行重命名
                        if filename == 'base_256.jpg':
                            new_name = '1080_base_256.jpg'
                            new_path = os.path.join(foldername, new_name)
                            os.replace(file_path, new_path)
                            print(f"已重命名: {file_path} → {new_name}")

                except Exception as e:
                    print(f"处理 {file_path} 时出错: {str(e)}")

# def check_ffmpeg():
#     """检查系统是否安装了 ffmpeg"""
#     try:
#         subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
#         return True
#     except (subprocess.CalledProcessError, FileNotFoundError):
#         return False

def ms_to_time(ms):
    """将毫秒转换为 ffmpeg 时间格式 (HH:MM:SS.mmm)"""
    seconds = ms / 1000.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"

def ms_to_seconds(ms):
    """将毫秒转换为秒"""
    return ms / 1000.0

def crop_audio_fade(input_file, output_file, start_ms, end_ms, fade_in_ms=500, fade_out_ms=2000, ffmpeg_path="E:/ffmpeg/bin/ffmpeg.exe"):
    """使用WAV中间格式实现可靠的淡入淡出效果"""
    start_sec = ms_to_seconds(start_ms)
    end_sec = ms_to_seconds(end_ms)
    fade_out_sec = ms_to_seconds(fade_out_ms)
    fade_in_sec = ms_to_seconds(fade_in_ms)
    
    duration = end_sec - start_sec
    if duration <= 0:
        print(f"  错误：结束时间必须大于开始时间 ({start_ms}ms - {end_ms}ms)")
        return False

    fade_out_start = max(0, duration - fade_out_sec)


    
    try:
        # 创建临时文件
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_file:
            temp_wav = temp_file.name
        
        # 第一步：裁剪并转换为WAV
        cmd1 = [
            ffmpeg_path,
            "-i", input_file,
            "-ss", ms_to_time(start_ms),
            "-to", ms_to_time(end_ms),
            "-c:a", "pcm_s16le",  # 使用16位PCM WAV格式
            "-y",  # 覆盖输出文件
            temp_wav
        ]
        
        result1 = subprocess.run(
            cmd1, 
            capture_output=True, 
            check=True, 
            text=True, 
            encoding='utf-8',
            errors='ignore',
            timeout=60  # 设置超时
        )
        
        # 第二步：应用淡入淡出效果并转换为OGG
        cmd2 = [
            ffmpeg_path,
            "-i", temp_wav,
            "-af", f"afade=t=in:st=0:d={fade_in_sec},afade=t=out:st={fade_out_start}:d={fade_out_sec}",
            "-c:a", "libvorbis",
            "-y",  # 覆盖输出文件
            output_file
        ]
        
        result2 = subprocess.run(
            cmd2, 
            capture_output=True, 
            check=True, 
            text=True, 
            encoding='utf-8',
            errors='ignore',
            timeout=60  # 设置超时
        )
        
        # 清理临时文件
        try:
            os.unlink(temp_wav)
        except:
            pass
            
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"  FFmpeg 错误: {e}")
        try:
            error_msg = e.stderr if isinstance(e.stderr, str) else e.stderr.decode('utf-8', errors='ignore')
            print(f"  FFmpeg 错误信息: {error_msg[:500]}...")
        except:
            print("  无法解析错误信息")
        
        # 清理临时文件
        try:
            if 'temp_wav' in locals():
                os.unlink(temp_wav)
        except:
            pass
            
        return False
        
    except subprocess.TimeoutExpired:
        print("  FFmpeg 处理超时")
        # 清理临时文件
        try:
            if 'temp_wav' in locals():
                os.unlink(temp_wav)
        except:
            pass
        return False
        
    except Exception as e:
        print(f"  未知错误: {e}")
        # 清理临时文件
        try:
            if 'temp_wav' in locals():
                os.unlink(temp_wav)
        except:
            pass
        return False


def crop_audio_with_reencode(input_file, output_file, start_ms, end_ms, ffmpeg_path="E:/ffmpeg/bin/ffmpeg.exe"):
    """使用重新编码的方法处理音频（更慢但更兼容）"""
    start_time = ms_to_time(start_ms)
    end_time = ms_to_time(end_ms)
    duration = (end_ms - start_ms) / 1000.0
    
    if duration <= 0:
        return False
    
    # 使用重新编码而不是流复制
    cmd = [
        ffmpeg_path,
        "-i", input_file,
        "-ss", start_time,
        "-to", end_time,
        "-c:a", "libvorbis",  # 明确指定音频编码器
        "-y",  # 覆盖输出文件
        output_file
    ]
    
    try:
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            check=True, 
            text=True, 
            encoding='utf-8',
            errors='ignore'
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"  重新编码方法也失败: {e}")
        try:
            error_msg = e.stderr if isinstance(e.stderr, str) else e.stderr.decode('utf-8', errors='ignore')
            print(f"  详细错误: {error_msg[:500]}...")
        except:
            pass
        return False

def validate_audio_file(file_path, ffmpeg_path="E:/ffmpeg/bin/ffmpeg.exe"):
    """验证音频文件是否有效"""
    try:
        cmd = [ffmpeg_path, "-v", "error", "-i", str(file_path), "-f", "null", "-"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=30  # 设置超时防止卡住
        )
        return result.returncode == 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False

def process_songlist(songlist_path, base_path):
    """处理 songlist 文件并创建预览文件"""
    
    # 读取 songlist 文件
    try:
        with open(songlist_path, 'r', encoding='utf-8') as f:
            songlist_data = json.load(f)
    except Exception as e:
        print(f"错误：无法读取或解析 songlist 文件: {e}")
        return False
    
    # 检查 songs 条目
    if "songs" not in songlist_data:
        print("错误：songlist 文件中没有 'songs' 条目")
        return False
    
    songs = songlist_data["songs"]
    if not isinstance(songs, list):
        print("错误：'songs' 条目不是列表")
        return False
    
    processed_count = 0
    skipped_count = 0
    error_count = 0
    deleted_base_count = 0
    
    print(f"找到 {len(songs)} 首歌曲需要处理")
    print("-" * 60)
    
    for i, song in enumerate(songs, 1):
        # 检查必要的字段
        if not all(key in song for key in ["id", "audioPreview", "audioPreviewEnd"]):
            print(f"{i}/{len(songs)} 跳过：缺少必要字段 (id, audioPreview, audioPreviewEnd)")
            error_count += 1
            continue
        
        song_id = song["id"]
        start_ms = song["audioPreview"]
        end_ms = song["audioPreviewEnd"]
        
        print(f"{i}/{len(songs)} 处理歌曲: {song_id} ({start_ms}ms - {end_ms}ms)")
        
        # 查找对应的文件夹
        song_folder = None
        if song_id == "sayonarahatsukoi":
            processed_count += 1
            continue
        else:
            song_id = "dl_" + song_id
            for item in base_path.iterdir():
                if item.is_dir() and item.name == song_id:
                    song_folder = item
                    break
        
        if not song_folder:
            print(f"  警告：找不到 ID 为 '{song_id}' 的文件夹")
            error_count += 1
            continue

        # 检查是否已存在 preview.ogg
        preview_file = song_folder / "preview.ogg"
        if preview_file.exists():
            print(f"  跳过：preview.ogg 已存在")
            skipped_count += 1
            continue
        
        # 检查 base.ogg 是否存在
        base_file = song_folder / "base.ogg"
        if not base_file.exists():
            print(f"  警告：base.ogg 不存在于文件夹 {song_id}")
            error_count += 1
            continue
        
        # 裁剪音频
        print(f"  裁剪: {base_file} -> {preview_file}")
        success = crop_audio_fade(str(base_file), str(preview_file), start_ms, end_ms)

        if success:
            if preview_file.exists() and preview_file.stat().st_size > 0:
                if validate_audio_file(preview_file):
                    file_size = preview_file.stat().st_size
                    print(f"  成功创建预览文件 ({file_size} 字节)")

                    try:
                        base_file.unlink()  # 删除文件
                        print(f"  已删除 base.ogg")
                        deleted_base_count += 1
                    except Exception as e:
                        print(f"  警告：无法删除 base.ogg: {e}")
                
                    processed_count += 1
                else:
                    print("  警告：生成的预览文件无效，已删除")
                    try:
                        preview_file.unlink()
                    except:
                        pass
                    error_count += 1
            else:
                print("  错误：预览文件创建失败")                
                error_count += 1
        else:
            error_count += 1
        
        print()

        # 额外处理3.ogg音频
        three_file = song_folder / "3.ogg"
        three_preview_file = song_folder / "3_preview.ogg"
        if three_file.exists():
            if three_preview_file.exists():
                print(f"  跳过：3_preview.ogg 已存在")
                skipped_count += 1
                continue
            else:
                start_ms = song["difficulties"][3]["audioPreview"] if "audioPreview" in song["difficulties"][3] else song["audioPreview"]
                end_ms = song["difficulties"][3]["audioPreviewEnd"] if "audioPreviewEnd" in song["difficulties"][3] else song["audioPreviewEnd"]
                print(f"  裁剪: {three_file} -> {three_preview_file}")
                three_success = crop_audio_fade(str(three_file), str(three_preview_file), start_ms, end_ms)
                if three_success:
                    if three_preview_file.exists() and three_preview_file.stat().st_size > 0:
                        if validate_audio_file(three_preview_file):
                            file_size = three_preview_file.stat().st_size
                            print(f"  成功创建预览文件 ({file_size} 字节)")

                            try:
                                three_file.unlink()  # 删除文件
                                print(f"  已删除 3.ogg")
                                deleted_base_count += 1
                            except Exception as e:
                                print(f"  警告：无法删除 3.ogg: {e}")
                        
                            processed_count += 1
                        else:
                            print("  警告：生成的预览文件无效，已删除")
                            try:
                                three_preview_file.unlink()
                            except:
                                pass
                            error_count += 1
                    else:
                        print("  错误：预览文件创建失败")                
                        error_count += 1
                else:
                    error_count += 1
                continue


    # 输出统计信息
    print("=" * 60)
    print(f"处理完成！")
    print(f"成功处理: {processed_count}")
    print(f"跳过（已存在）: {skipped_count}")
    print(f"错误/失败: {error_count}")
    
    return True

if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='处理目标文件夹下的图片尺寸，并根据 songlist 创建音频预览文件')
    parser.add_argument('path', nargs='?', default='./active/songs', 
                       help='目标路径（默认为./active/songs）')
    parser.add_argument('--songlist', default='songlist',
                       help='songlist 文件名（默认为 "path/songlist"）')
    
    args = parser.parse_args()
    
    base_path = Path(args.path)
    songlist_path = base_path / args.songlist

    
    # 检查路径和文件是否存在
    if not base_path.exists():
        print(f"错误：路径 '{base_path}' 不存在")
        sys.exit(1)
    
    if not songlist_path.exists():
        print(f"错误：songlist 文件 '{songlist_path}' 不存在")
        sys.exit(1)
    
    print(f"工作路径: {base_path}")
    print(f"Songlist 文件: {songlist_path}")
    print()

    print("开始处理图片...")
    process_images(base_path)
    print("图片处理完成！")

    # 开始处理
    process_songlist(songlist_path, base_path)