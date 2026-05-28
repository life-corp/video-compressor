#!/usr/bin/env python3
"""
Video Compressor GUI - All-in-one video compression application
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import os
import sys
import json
import re
import subprocess
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class VideoInfo:
    """Container for video metadata."""
    duration: float  # seconds
    width: int
    height: int
    fps: float
    video_bitrate: Optional[int]  # kbps
    audio_bitrate: Optional[int]  # kbps


class VideoCompressor:
    """Video compression engine with support for multiple encoders."""
    
    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        
    def log(self, message):
        """Log a message."""
        if self.log_callback:
            self.log_callback(message)
        else:
            print(message, end='')
    
    def parse_size(self, size_str: str) -> int:
        """Parse a size string like '10MB' or '500KB' into bytes."""
        size_str = size_str.strip().upper()
        
        units = {
            'B': 1,
            'KB': 1024,
            'K': 1024,
            'MB': 1024 ** 2,
            'M': 1024 ** 2,
            'GB': 1024 ** 3,
            'G': 1024 ** 3,
        }
        
        match = re.match(r'^([\d.]+)\s*([A-Z]+)?$', size_str)
        if not match:
            raise ValueError(f"Invalid size format: {size_str}")
        
        value = float(match.group(1))
        unit = match.group(2) or 'B'
        
        if unit not in units:
            raise ValueError(f"Unknown unit: {unit}")
        
        return int(value * units[unit])
    
    def get_video_info(self, input_file: str, invert_aspect_ratio: bool) -> VideoInfo:
        """Extract video information using ffprobe."""
        # Try both 'ffprobe' and 'ffprobe.exe'
        ffprobe_cmd = 'ffprobe.exe' if os.path.exists('./ffprobe.exe') else 'ffprobe'
        
        cmd = [
            ffprobe_cmd,
            '-v', 'quiet',
            '-print_format', 'json',
            '-show_format',
            '-show_streams',
            input_file
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {result.stderr}")
        
        data = json.loads(result.stdout)
        
        # Extract video and audio streams
        video_stream = None
        audio_stream = None
        
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video' and video_stream is None:
                video_stream = stream
            elif stream.get('codec_type') == 'audio' and audio_stream is None:
                audio_stream = stream
        
        if not video_stream:
            raise RuntimeError("No video stream found in input file")
        
        # Extract duration
        duration = float(data.get('format', {}).get('duration', 0))
        if duration == 0:
            duration = float(video_stream.get('duration', 0))
        
        if duration == 0:
            raise RuntimeError("Could not determine video duration")
        
        # Extract video height/width
        width = int(video_stream.get('width', 0))
        height = int(video_stream.get('height', 0))
        
        # If user requested to invert the aspect ratio, flip width and height
        if invert_aspect_ratio:
            width, height = height, width
        
        # Extract fps
        fps_str = video_stream.get('r_frame_rate', '30/1')
        if '/' in fps_str:
            num, den = fps_str.split('/')
            fps = float(num) / float(den) if float(den) != 0 else 30.0
        else:
            fps = float(fps_str)
        
        # Extract bitrates (optional)
        video_bitrate = None
        if 'bit_rate' in video_stream:
            video_bitrate = int(video_stream['bit_rate']) // 1000
        
        audio_bitrate = None
        if audio_stream and 'bit_rate' in audio_stream:
            audio_bitrate = int(audio_stream['bit_rate']) // 1000
        
        return VideoInfo(
            duration=duration,
            width=width,
            height=height,
            fps=fps,
            video_bitrate=video_bitrate,
            audio_bitrate=audio_bitrate
        )
    
    def calculate_optimal_params(
        self,
        video_info: VideoInfo,
        target_bytes: int,
        audio_bitrate_kbps: int = 16,
        fixed_fps: Optional[float] = None,
        codec: str = "hevc"
    ) -> Tuple[int, int, float, int]:
        """
        Calculate optimal encoding parameters for target file size.
        
        Returns: (width, height, fps, video_bitrate_kbps)
        """
        # Reserve some space for container overhead (~2%)
        usable_bytes = int(target_bytes * 0.98)
        
        # Calculate total available bitrate in kbps
        total_bitrate_kbps = (usable_bytes * 8) / (video_info.duration * 1000)
        
        # Subtract audio bitrate to get video bitrate budget
        video_bitrate_kbps = total_bitrate_kbps - audio_bitrate_kbps
        
        # Determine effective FPS for minimum bitrate calculation
        effective_fps = fixed_fps if fixed_fps is not None else video_info.fps
        fps_factor = effective_fps / 30.0
        
        # Set minimum bitrates based on codec efficiency
        if codec == "h264":
            min_video_bitrate = max(75 * fps_factor, 30)
            resolution_tiers = [
                (1920, 1080, 2500, 6000),
                (1280, 720, 1200, 4000),
                (854, 480, 600, 1800),
                (640, 360, 300, 1000),
                (426, 240, 150, 600),
                (320, 180, 75, 300),
            ]
        else:  # hevc or av1
            min_video_bitrate = max(50 * fps_factor, 20)
            resolution_tiers = [
                (1920, 1080, 1500, 4000),
                (1280, 720, 800, 2500),
                (854, 480, 400, 1200),
                (640, 360, 200, 650),
                (426, 240, 100, 400),
                (320, 180, 50, 200),
            ]
        
        fps_tiers = [60, 30, 24, 15, 10]
        
        # Calculate original aspect ratio
        aspect_ratio = video_info.width / video_info.height
        
        # Find the best resolution that fits the bitrate budget
        best_width, best_height = resolution_tiers[-1][0], resolution_tiers[-1][1]
        
        for width, height, min_br, ideal_br in resolution_tiers:
            # Adjust dimensions to match source aspect ratio
            if aspect_ratio > width / height:
                adjusted_height = int(width / aspect_ratio)
                adjusted_height = adjusted_height - (adjusted_height % 2)
                adjusted_width = width
            else:
                adjusted_width = int(height * aspect_ratio)
                adjusted_width = adjusted_width - (adjusted_width % 2)
                adjusted_height = height
            
            # Don't upscale
            if adjusted_width > video_info.width or adjusted_height > video_info.height:
                adjusted_width = video_info.width - (video_info.width % 2)
                adjusted_height = video_info.height - (video_info.height % 2)
            
            # Scale bitrate requirements based on actual resolution vs reference
            pixel_ratio = (adjusted_width * adjusted_height) / (width * height)
            scaled_min_br = min_br * pixel_ratio
            
            if video_bitrate_kbps >= scaled_min_br:
                best_width, best_height = adjusted_width, adjusted_height
                break
        
        # Handle FPS: use fixed value if provided, otherwise calculate optimal
        if fixed_fps is not None:
            best_fps = fixed_fps
        else:
            # Find the best FPS that doesn't waste bitrate
            best_fps = fps_tiers[-1]
            
            for fps in fps_tiers:
                if fps > video_info.fps:
                    continue
                
                pixels_per_sec = best_width * best_height * fps
                bpp = (video_bitrate_kbps * 1000) / pixels_per_sec
                
                # Codec-specific BPP thresholds
                min_bpp = 0.03 if codec == "h264" else 0.02
                if bpp >= min_bpp:
                    best_fps = fps
                    break
        
        video_bitrate_kbps = int(video_bitrate_kbps)
        
        return best_width, best_height, best_fps, video_bitrate_kbps
    
    def get_encoder_params(self, codec: str, two_pass: bool, bitrate_kbps: int, 
                          bufsize_kbps: int, passlog: str = None) -> dict:
        """Get encoder-specific parameters."""
        params = {
            'base_args': [],
            'pass1_args': [],
            'pass2_args': [],
            'single_args': []
        }
        
        if codec == "h264":
            params['base_args'] = [
                '-c:v', 'libx264',
                '-preset', 'slow',
                '-profile:v', 'high',
                '-pix_fmt', 'yuv420p',
            ]
            x264opts = 'aq-mode=3:aq-strength=1.0:psy-rd=1.0,0.15:rc-lookahead=40:bframes=3:b-adapt=2:subme=9:deblock=-1,-1'
            
            if two_pass:
                params['pass1_args'] = [
                    '-b:v', f'{bitrate_kbps}k',
                    '-maxrate', f'{int(bitrate_kbps * 1.5)}k',
                    '-bufsize', f'{bufsize_kbps}k',
                    '-pass', '1',
                    '-passlogfile', passlog,
                    '-x264opts', x264opts,
                ]
                params['pass2_args'] = [
                    '-b:v', f'{bitrate_kbps}k',
                    '-maxrate', f'{int(bitrate_kbps * 1.5)}k',
                    '-bufsize', f'{bufsize_kbps}k',
                    '-pass', '2',
                    '-passlogfile', passlog,
                    '-x264opts', x264opts,
                ]
            else:
                params['single_args'] = [
                    '-b:v', f'{bitrate_kbps}k',
                    '-maxrate', f'{int(bitrate_kbps * 1.5)}k',
                    '-bufsize', f'{bufsize_kbps}k',
                    '-x264opts', x264opts,
                ]
        
        elif codec == "hevc_cpu":
            params['base_args'] = [
                '-c:v', 'libx265',
                '-preset', 'slow',
                '-profile:v', 'main',
                '-pix_fmt', 'yuv420p',
                '-tag:v', 'hvc1',
            ]
            x265params = 'aq-mode=3:aq-strength=1.0:psy-rd=2.0:psy-rdoq=1.0:rc-lookahead=32:bframes=4:b-adapt=2:subme=7:deblock=-1,-1'
            
            if two_pass:
                params['pass1_args'] = [
                    '-b:v', f'{bitrate_kbps}k',
                    '-maxrate', f'{int(bitrate_kbps * 1.5)}k',
                    '-bufsize', f'{bufsize_kbps}k',
                    '-x265-params', f'pass=1:stats={passlog}:{x265params}',
                ]
                params['pass2_args'] = [
                    '-b:v', f'{bitrate_kbps}k',
                    '-maxrate', f'{int(bitrate_kbps * 1.5)}k',
                    '-bufsize', f'{bufsize_kbps}k',
                    '-x265-params', f'pass=2:stats={passlog}:{x265params}',
                ]
            else:
                params['single_args'] = [
                    '-b:v', f'{bitrate_kbps}k',
                    '-maxrate', f'{int(bitrate_kbps * 1.5)}k',
                    '-bufsize', f'{bufsize_kbps}k',
                    '-x265-params', x265params,
                ]
        
        elif codec == "hevc_gpu":
            params['base_args'] = [
                '-c:v', 'hevc_nvenc',
                '-preset', 'p7',
                '-tune', 'hq',
                '-profile:v', 'main',
                '-pix_fmt', 'yuv420p',
                '-rc', 'vbr',
                '-spatial-aq', '1',
                '-temporal-aq', '1',
                '-aq-strength', '8',
                '-rc-lookahead', '32',
                '-b_ref_mode', 'middle',
                '-bf', '4',
                '-tag:v', 'hvc1',
            ]
            
            if two_pass:
                params['pass1_args'] = [
                    '-b:v', f'{bitrate_kbps}k',
                    '-maxrate', f'{int(bitrate_kbps * 1.5)}k',
                    '-bufsize', f'{bufsize_kbps}k',
                    '-multipass', 'fullres',
                ]
                params['pass2_args'] = [
                    '-b:v', f'{bitrate_kbps}k',
                    '-maxrate', f'{int(bitrate_kbps * 1.5)}k',
                    '-bufsize', f'{bufsize_kbps}k',
                    '-multipass', 'fullres',
                ]
            else:
                params['single_args'] = [
                    '-b:v', f'{bitrate_kbps}k',
                    '-maxrate', f'{int(bitrate_kbps * 1.5)}k',
                    '-bufsize', f'{bufsize_kbps}k',
                    '-multipass', 'fullres',
                ]
        
        elif codec == "av1_gpu":
            params['base_args'] = [
                '-c:v', 'av1_nvenc',
                '-preset', 'p7',
                '-tune', 'hq',
                '-pix_fmt', 'yuv420p',
                '-rc', 'vbr',
                '-spatial-aq', '1',
                '-temporal-aq', '1',
                '-aq-strength', '8',
                '-rc-lookahead', '32',
                '-bf', '4',
            ]
            
            if two_pass:
                params['pass1_args'] = [
                    '-b:v', f'{bitrate_kbps}k',
                    '-maxrate', f'{int(bitrate_kbps * 1.2)}k',
                    '-bufsize', f'{bufsize_kbps}k',
                    '-multipass', 'fullres',
                ]
                params['pass2_args'] = [
                    '-b:v', f'{bitrate_kbps}k',
                    '-maxrate', f'{int(bitrate_kbps * 1.2)}k',
                    '-bufsize', f'{bufsize_kbps}k',
                    '-multipass', 'fullres',
                ]
            else:
                params['single_args'] = [
                    '-b:v', f'{bitrate_kbps}k',
                    '-maxrate', f'{int(bitrate_kbps * 1.2)}k',
                    '-bufsize', f'{bufsize_kbps}k',
                    '-multipass', 'fullres',
                ]
        
        return params
    
    def encode_video(
        self,
        input_file: str,
        output_file: str,
        width: int,
        height: int,
        fps: float,
        video_bitrate_kbps: int,
        audio_bitrate_kbps: int,
        codec: str,
        two_pass: bool = True,
        target_bytes: Optional[int] = None,
        size_tolerance: float = 0.05
    ) -> bool:
        """Encode video with specified parameters."""
        
        def run_encode(bitrate_kbps: int, output_path: str, pass_name: str = "Encoding") -> bool:
            """Run the actual encode with given bitrate."""
            bufsize_kbps = max(bitrate_kbps, 300)
            vf_string = f'scale={width}:{height}:flags=lanczos,fps={fps}'
            
            # Create temp directory for pass logs
            tmpdir = 'tmp/'
            if not os.path.exists(tmpdir):
                os.makedirs(tmpdir)
            passlog = os.path.join(tmpdir, 'ffmpeg2pass')
            
            # Try both 'ffmpeg' and 'ffmpeg.exe'
            ffmpeg_cmd = 'ffmpeg.exe' if os.path.exists('./ffmpeg.exe') else 'ffmpeg'
            
            # Get encoder-specific parameters
            encoder_params = self.get_encoder_params(codec, two_pass, bitrate_kbps, bufsize_kbps, passlog)
            
            # Base arguments common to all encoders
            base_args = [
                ffmpeg_cmd,
                '-y',
                '-i', input_file,
                '-vf', vf_string,
            ] + encoder_params['base_args'] + [
                '-c:a', 'aac',
                '-ac', '1',
                '-ar', '22050',
                '-b:a', f'{audio_bitrate_kbps}k',
                '-movflags', '+faststart',
            ]
            
            if two_pass:
                # First pass (analysis)
                self.log(f"{pass_name}: Running analysis pass...\n")
                pass1_args = [
                    ffmpeg_cmd,
                    '-y',
                    '-i', input_file,
                    '-vf', vf_string,
                ] + encoder_params['base_args'] + encoder_params['pass1_args'] + [
                    '-an',
                    '-f', 'null',
                    '/dev/null' if os.name != 'nt' else 'NUL'
                ]
                
                result = subprocess.run(pass1_args, capture_output=True, text=True)
                if result.returncode != 0:
                    self.log(f"Analysis pass failed: {result.stderr}\n")
                    return False
                
                # Second pass (encode)
                self.log(f"{pass_name}: Running encode pass...\n")
                pass2_args = base_args + encoder_params['pass2_args'] + [output_path]
                
                result = subprocess.run(pass2_args, capture_output=True, text=True)
                if result.returncode != 0:
                    self.log(f"Encode pass failed: {result.stderr}\n")
                    return False
            else:
                # Single pass
                self.log(f"{pass_name}...\n")
                single_args = base_args + encoder_params['single_args'] + [output_path]
                
                result = subprocess.run(single_args, capture_output=True, text=True)
                if result.returncode != 0:
                    self.log(f"Encoding failed: {result.stderr}\n")
                    return False
            
            return True
        
        # If no target size specified, just do a normal encode
        if target_bytes is None:
            return run_encode(video_bitrate_kbps, output_file, "Encoding video")
        
        # Create temp directory for calibration encode
        tmpdir = 'tmp'
        if not os.path.exists(tmpdir):
            os.makedirs(tmpdir)
        temp_output = os.path.join(tmpdir, 'calibration_encode.mp4')
        
        # First encode: calibration pass
        self.log("Pass 1/2: Calibration encode...\n")
        if not run_encode(video_bitrate_kbps, temp_output, "Calibration"):
            return False
        
        # Measure actual size
        actual_size = os.path.getsize(temp_output)
        size_ratio = target_bytes / actual_size
        size_diff = abs(1.0 - size_ratio)
        
        self.log(f"  Calibration result: {actual_size / 1024:.1f} KB (target: {target_bytes / 1024:.1f} KB)\n")
        self.log(f"  Ratio: {size_ratio:.3f} (diff: {size_diff * 100:.1f}%)\n")
        
        # If within tolerance, just move the temp file
        if size_diff <= size_tolerance:
            self.log(f"  Within {size_tolerance * 100:.0f}% tolerance, using calibration encode.\n")
            import shutil
            shutil.move(temp_output, output_file)
            return True
        
        # Calculate adjusted bitrate
        adjusted_bitrate = int(video_bitrate_kbps * size_ratio)
        self.log(f"  Adjusting bitrate: {video_bitrate_kbps} -> {adjusted_bitrate} kbps\n")
        
        # Final encode with adjusted bitrate
        self.log("Pass 2/2: Final encode with adjusted bitrate...\n")
        success = run_encode(adjusted_bitrate, output_file, "Final encode")
        
        # Clean up temp file
        if os.path.exists(temp_output):
            os.remove(temp_output)
        
        return success
    
    def format_size(self, bytes_val: int) -> str:
        """Format bytes as human-readable string."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_val < 1024:
                return f"{bytes_val:.2f} {unit}"
            bytes_val /= 1024
        return f"{bytes_val:.2f} TB"
    
    def compress(
        self,
        input_file: str,
        output_file: str,
        target_size_kb: float,
        fps: float,
        format_choice: str,
        audio_bitrate: int = 16,
        single_pass: bool = False,
        invert_aspect: bool = False
    ) -> bool:
        """Main compression function."""
        try:
            # Map format choice to codec identifier
            codec_map = {
                "H.264 (CPU)": "h264",
                "HEVC (CPU)": "hevc_cpu",
                "HEVC (GPU)": "hevc_gpu",
                "AV1 (GPU)": "av1_gpu"
            }
            codec = codec_map.get(format_choice, "hevc_cpu")
            
            # Parse target size
            target_bytes = int(target_size_kb * 1024)
            self.log(f"Target file size: {self.format_size(target_bytes)}\n")
            
            # Get video info
            self.log(f"Analyzing input video: {input_file}\n")
            video_info = self.get_video_info(input_file, invert_aspect)
            
            self.log(f"  Duration: {video_info.duration:.2f}s\n")
            self.log(f"  Resolution: {video_info.width}x{video_info.height}\n")
            self.log(f"  FPS: {video_info.fps:.2f}\n")
            
            # Warn if fixed FPS exceeds source FPS
            if fps > video_info.fps:
                self.log(f"  ⚠️  Warning: Requested FPS ({fps}) exceeds source FPS ({video_info.fps:.2f})\n")
            
            # Calculate optimal parameters
            width, height, output_fps, video_bitrate = self.calculate_optimal_params(
                video_info,
                target_bytes,
                audio_bitrate,
                fixed_fps=fps,
                codec=codec.replace("_cpu", "").replace("_gpu", "")
            )
            
            self.log(f"\nOptimal encoding parameters:\n")
            self.log(f"  Resolution: {width}x{height}\n")
            self.log(f"  FPS: {output_fps} (fixed)\n")
            self.log(f"  Video bitrate: {video_bitrate} kbps\n")
            self.log(f"  Audio bitrate: {audio_bitrate} kbps\n")
            self.log(f"  Encoder: {format_choice}\n")
            
            # Calculate expected file size
            expected_size = int((video_bitrate + audio_bitrate) * video_info.duration * 1000 / 8)
            self.log(f"  Expected size: ~{self.format_size(expected_size)}\n")
            
            # Encode video
            self.log(f"\nEncoding to: {output_file}\n")
            success = self.encode_video(
                input_file,
                output_file,
                width,
                height,
                output_fps,
                video_bitrate,
                audio_bitrate,
                codec,
                two_pass=not single_pass,
                target_bytes=target_bytes
            )
            
            if not success:
                self.log("Encoding failed!\n")
                return False
            
            # Report results
            actual_size = os.path.getsize(output_file)
            self.log(f"\nEncoding complete!\n")
            self.log(f"  Target size: {self.format_size(target_bytes)}\n")
            self.log(f"  Actual size: {self.format_size(actual_size)}\n")
            self.log(f"  Difference: {(actual_size - target_bytes) / target_bytes * 100:+.1f}%\n")
            
            return True
            
        except Exception as e:
            self.log(f"Error: {str(e)}\n")
            return False


class VideoCompressorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Video Compressor")
        self.root.geometry("850x750")
        
        # Variables
        self.input_file = tk.StringVar()
        self.output_folder = tk.StringVar()
        self.output_filename = tk.StringVar(value="compressed_video.mp4")
        self.target_size = tk.StringVar(value="640")
        self.fps = tk.StringVar(value="24")
        self.format_choice = tk.StringVar(value="HEVC (CPU)")
        self.audio_bitrate = tk.StringVar(value="32")
        self.single_pass = tk.BooleanVar(value=False)
        self.invert_aspect = tk.BooleanVar(value=False)
        
        # Processing flag
        self.is_processing = False
        
        self.create_widgets()
        
    def create_widgets(self):
        """Create all GUI widgets"""
        # Main container
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # Configure grid weights
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        
        row = 0
        
        # Title
        title_label = ttk.Label(main_frame, text="Video Compressor", font=("Arial", 16, "bold"))
        title_label.grid(row=row, column=0, columnspan=3, pady=(0, 10))
        row += 1
        
        # Input file selection
        ttk.Label(main_frame, text="Input File:").grid(row=row, column=0, sticky=tk.W, pady=5)
        ttk.Entry(main_frame, textvariable=self.input_file, width=50).grid(row=row, column=1, sticky=(tk.W, tk.E), padx=5)
        ttk.Button(main_frame, text="Browse...", command=self.browse_input).grid(row=row, column=2, pady=5)
        row += 1
        
        # Output folder selection
        ttk.Label(main_frame, text="Output Folder:").grid(row=row, column=0, sticky=tk.W, pady=5)
        ttk.Entry(main_frame, textvariable=self.output_folder, width=50).grid(row=row, column=1, sticky=(tk.W, tk.E), padx=5)
        ttk.Button(main_frame, text="Browse...", command=self.browse_output_folder).grid(row=row, column=2, pady=5)
        row += 1
        
        # Output filename
        ttk.Label(main_frame, text="Output Filename:").grid(row=row, column=0, sticky=tk.W, pady=5)
        ttk.Entry(main_frame, textvariable=self.output_filename, width=50).grid(row=row, column=1, sticky=(tk.W, tk.E), padx=5)
        row += 1
        
        # Separator
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=10)
        row += 1
        
        # Format selection
        ttk.Label(main_frame, text="Output Format:").grid(row=row, column=0, sticky=tk.W, pady=5)
        format_combo = ttk.Combobox(main_frame, textvariable=self.format_choice, 
                                     values=["H.264 (CPU)", "HEVC (CPU)", "HEVC (GPU)", "AV1 (GPU)"],
                                     state="readonly", width=20)
        format_combo.grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1
        
        # Target size
        ttk.Label(main_frame, text="Target Size (KB):").grid(row=row, column=0, sticky=tk.W, pady=5)
        size_frame = ttk.Frame(main_frame)
        size_frame.grid(row=row, column=1, sticky=tk.W, padx=5)
        ttk.Entry(size_frame, textvariable=self.target_size, width=15).pack(side=tk.LEFT)
        ttk.Label(size_frame, text="KB", foreground="gray").pack(side=tk.LEFT, padx=(5, 0))
        row += 1
        
        # FPS
        ttk.Label(main_frame, text="FPS:").grid(row=row, column=0, sticky=tk.W, pady=5)
        fps_frame = ttk.Frame(main_frame)
        fps_frame.grid(row=row, column=1, sticky=tk.W, padx=5)
        ttk.Entry(fps_frame, textvariable=self.fps, width=15).pack(side=tk.LEFT)
        ttk.Label(fps_frame, text="frames/second", foreground="gray").pack(side=tk.LEFT, padx=(5, 0))
        row += 1
        
        # Separator
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=10)
        row += 1
        
        # Advanced options label
        ttk.Label(main_frame, text="Advanced Options:", font=("Arial", 10, "bold")).grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(5, 5))
        row += 1
        
        # Audio bitrate
        ttk.Label(main_frame, text="Audio Bitrate (kbps):").grid(row=row, column=0, sticky=tk.W, pady=5)
        audio_frame = ttk.Frame(main_frame)
        audio_frame.grid(row=row, column=1, sticky=tk.W, padx=5)
        ttk.Entry(audio_frame, textvariable=self.audio_bitrate, width=15).pack(side=tk.LEFT)
        ttk.Label(audio_frame, text="kbps (default: 16)", foreground="gray").pack(side=tk.LEFT, padx=(5, 0))
        row += 1
        
        # Checkboxes
        ttk.Checkbutton(main_frame, text="Single-pass encoding (faster, less accurate)", 
                       variable=self.single_pass).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=2)
        row += 1
        
        ttk.Checkbutton(main_frame, text="Invert aspect ratio (if video appears squashed)", 
                       variable=self.invert_aspect).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=2)
        row += 1
        
        # Separator
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=10)
        row += 1
        
        # Convert button
        self.convert_button = ttk.Button(main_frame, text="Compress Video", command=self.start_compression, style="Accent.TButton")
        self.convert_button.grid(row=row, column=0, columnspan=3, pady=10)
        row += 1
        
        # Progress bar
        self.progress = ttk.Progressbar(main_frame, mode='indeterminate')
        self.progress.grid(row=row, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=5)
        row += 1
        
        # Output log
        ttk.Label(main_frame, text="Output Log:", font=("Arial", 10, "bold")).grid(row=row, column=0, sticky=tk.W, pady=(10, 5))
        row += 1
        
        self.log_text = scrolledtext.ScrolledText(main_frame, height=15, wrap=tk.WORD)
        self.log_text.grid(row=row, column=0, columnspan=3, sticky=(tk.W, tk.E, tk.N, tk.S), pady=5)
        main_frame.rowconfigure(row, weight=1)
        
        # Style for accent button
        style = ttk.Style()
        style.configure("Accent.TButton", font=("Arial", 11, "bold"))
        
    def browse_input(self):
        """Browse for input video file"""
        filename = filedialog.askopenfilename(
            title="Select Input Video",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.avi *.mkv *.webm *.flv"),
                ("All files", "*.*")
            ]
        )
        if filename:
            self.input_file.set(filename)
            
            # Auto-suggest output folder if not set
            if not self.output_folder.get():
                input_path = Path(filename)
                self.output_folder.set(str(input_path.parent))
            
            # Auto-suggest output filename
            if self.output_filename.get() == "compressed_video.mp4":
                input_path = Path(filename)
                self.output_filename.set(f"{input_path.stem}_compressed.mp4")
    
    def browse_output_folder(self):
        """Browse for output folder"""
        folder = filedialog.askdirectory(title="Select Output Folder")
        if folder:
            self.output_folder.set(folder)
    
    def log_output(self, message):
        """Add message to log output"""
        self.log_text.insert(tk.END, message)
        self.log_text.see(tk.END)
        self.root.update_idletasks()
    
    def validate_inputs(self):
        """Validate all input fields"""
        if not self.input_file.get():
            messagebox.showerror("Error", "Please select an input file")
            return False
        
        if not os.path.exists(self.input_file.get()):
            messagebox.showerror("Error", "Input file does not exist")
            return False
        
        if not self.output_folder.get():
            messagebox.showerror("Error", "Please select an output folder")
            return False
        
        if not os.path.exists(self.output_folder.get()):
            messagebox.showerror("Error", "Output folder does not exist")
            return False
        
        if not self.output_filename.get():
            messagebox.showerror("Error", "Please enter an output filename")
            return False
        
        try:
            size = float(self.target_size.get())
            if size <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Target size must be a positive number")
            return False
        
        try:
            fps_val = float(self.fps.get())
            if fps_val <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "FPS must be a positive number")
            return False
        
        try:
            audio_br = int(self.audio_bitrate.get())
            if audio_br <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Audio bitrate must be a positive integer")
            return False
        
        return True
    
    def start_compression(self):
        """Start the compression process in a separate thread"""
        if self.is_processing:
            messagebox.showwarning("Warning", "A compression is already in progress")
            return
        
        if not self.validate_inputs():
            return
        
        # Clear log
        self.log_text.delete(1.0, tk.END)
        
        # Start compression in thread
        self.is_processing = True
        self.convert_button.config(state=tk.DISABLED)
        self.progress.start()
        
        thread = threading.Thread(target=self.run_compression)
        thread.daemon = True
        thread.start()
    
    def run_compression(self):
        """Run the compression"""
        try:
            
            ########################################################################
            # ACTUAL VIDEO COMPRESSION
            #
            # Build output path
            output_path = os.path.join(self.output_folder.get(), self.output_filename.get())
            
            # Create compressor with log callback
            compressor = VideoCompressor(log_callback=self.log_output)
            
            # Run compression
            success = compressor.compress(
                input_file=self.input_file.get(),
                output_file=output_path,
                target_size_kb=float(self.target_size.get()),
                fps=float(self.fps.get()),
                format_choice=self.format_choice.get(),
                audio_bitrate=int(self.audio_bitrate.get()),
                single_pass=self.single_pass.get(),
                invert_aspect=self.invert_aspect.get()
            )
            
            if success:
                self.log_output(f"\n{'=' * 80}\n")
                self.log_output("✓ Compression completed successfully!\n")
                messagebox.showinfo("Success", f"Video compression completed!\n\nOutput: {output_path}")
            else:
                self.log_output(f"\n{'=' * 80}\n")
                self.log_output("✗ Compression failed!\n")
                messagebox.showerror("Error", "Video compression failed. Check the log for details.")
    
            #########################################################################

        except Exception as e:
            self.log_output(f"\nError: {str(e)}\n")
            messagebox.showerror("Error", f"An error occurred: {str(e)}")
        
        finally:
            # Reset UI
            self.is_processing = False
            self.progress.stop()
            self.convert_button.config(state=tk.NORMAL)


def main():
    root = tk.Tk()
    app = VideoCompressorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()