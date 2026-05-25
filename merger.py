import os
import subprocess
import time


def merge_video_audio(video_path, audio_path, output_path):
    """
    Merges video and audio files using ffmpeg.
    """
    if not os.path.exists(video_path) or not os.path.exists(audio_path):
        print(f"Error: Required files missing for merge ({video_path} or {audio_path})")
        return False

    print(f"\n🔀 Merging {video_path} and {audio_path} into {output_path}...")
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-i",
                audio_path,
                "-c",
                "copy",
                output_path,
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            print(f"✅ Success! Saved as {output_path}")
            time.sleep(5)
            # Optional: remove temp files
            for f in [video_path, audio_path]:
                try:
                    os.remove(f)
                except:
                    pass
            return True
        else:
            print(f"❌ ffmpeg Error:\n{result.stderr}")
            return False
    except FileNotFoundError:
        print(
            "Error: ffmpeg not found. Please ensure ffmpeg is installed and in your PATH."
        )
        return False
