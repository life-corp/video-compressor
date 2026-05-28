#!/usr/bin/env python3

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Tuple


LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_FILE = "video_compress_api.log"
LOGGER = logging.getLogger(__name__)


@dataclass
class VideoInfo:
    """Container for video metadata."""
    duration: float  # seconds
    width: int
    height: int
    fps: float
    video_bitrate: Optional[int]  # kbps
    audio_bitrate: Optional[int]  # kbps


@dataclass
class RabbitMQConfig:
    """RabbitMQ connection settings for the compression worker."""
    host: str = "localhost"
    port: int = 5672
    virtual_host: str = "/"
    username: str = "guest"
    password: str = "guest"
    queue_name: str = "VideoCompression"
    heartbeat: int = 600
    blocked_connection_timeout: int = 300
    max_concurrent_jobs: int = 2
    prefetch_count: int = 2
    requeue_on_failure: bool = False

    @classmethod
    def from_env(cls) -> "RabbitMQConfig":
        """Create config from environment variables."""
        max_concurrent_jobs = int(os.getenv("VIDEO_COMPRESS_CONCURRENCY", cls.max_concurrent_jobs))
        prefetch_count = int(os.getenv("RABBITMQ_PREFETCH_COUNT", max_concurrent_jobs))

        return cls(
            host=os.getenv("RABBITMQ_HOST", cls.host),
            port=int(os.getenv("RABBITMQ_PORT", cls.port)),
            virtual_host=os.getenv("RABBITMQ_VHOST", cls.virtual_host),
            username=os.getenv("RABBITMQ_USER", cls.username),
            password=os.getenv("RABBITMQ_PASS", cls.password),
            queue_name=os.getenv("RABBITMQ_QUEUE", cls.queue_name),
            heartbeat=int(os.getenv("RABBITMQ_HEARTBEAT", cls.heartbeat)),
            blocked_connection_timeout=int(
                os.getenv("RABBITMQ_BLOCKED_CONNECTION_TIMEOUT", cls.blocked_connection_timeout)
            ),
            max_concurrent_jobs=max_concurrent_jobs,
            prefetch_count=prefetch_count,
            requeue_on_failure=os.getenv("RABBITMQ_REQUEUE_ON_FAILURE", "false").lower()
            in {"1", "true", "yes"},
        )


class VideoCompressor:
    """Reusable video compression API backed by ffmpeg/ffprobe."""

    DEFAULT_TARGET_SIZE_KB = 640
    DEFAULT_AUDIO_BITRATE_KBPS = 32
    DEFAULT_FPS = 24

    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        temp_dir: str = "tmp",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.temp_dir = temp_dir
        self.logger = logger or logging.getLogger(f"{__name__}.VideoCompressor")

    def log(self, message: str) -> None:
        """Log a status message."""
        self.logger.info(message)

    @staticmethod
    def parse_size(size_str: str) -> int:
        """Parse a size string like '10MB' or '500KB' into bytes."""
        size_str = size_str.strip().upper()

        units = {
            "B": 1,
            "KB": 1024,
            "K": 1024,
            "MB": 1024 ** 2,
            "M": 1024 ** 2,
            "GB": 1024 ** 3,
            "G": 1024 ** 3,
        }

        match = re.match(r"^([\d.]+)\s*([A-Z]+)?$", size_str)
        if not match:
            raise ValueError(f"Invalid size format: {size_str}")

        value = float(match.group(1))
        unit = match.group(2) or "B"

        if unit not in units:
            raise ValueError(f"Unknown unit: {unit}")

        return int(value * units[unit])

    def get_video_info(self, input_file: str, invert_aspect_ratio: bool = False) -> VideoInfo:
        """Extract video information using ffprobe."""
        cmd = [
            self.ffprobe_path,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            input_file,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {result.stderr}")

        data = json.loads(result.stdout)

        video_stream = None
        audio_stream = None

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video" and video_stream is None:
                video_stream = stream
            elif stream.get("codec_type") == "audio" and audio_stream is None:
                audio_stream = stream

        if not video_stream:
            raise RuntimeError("No video stream found in input file")

        duration = float(data.get("format", {}).get("duration", 0))
        if duration == 0:
            duration = float(video_stream.get("duration", 0))

        if duration == 0:
            raise RuntimeError("Could not determine video duration")

        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))

        if invert_aspect_ratio:
            width, height = height, width

        fps_str = video_stream.get("r_frame_rate", "30/1")
        if "/" in fps_str:
            num, den = fps_str.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 30.0
        else:
            fps = float(fps_str)

        video_bitrate = None
        if "bit_rate" in video_stream:
            video_bitrate = int(video_stream["bit_rate"]) // 1000

        audio_bitrate = None
        if audio_stream and "bit_rate" in audio_stream:
            audio_bitrate = int(audio_stream["bit_rate"]) // 1000

        return VideoInfo(
            duration=duration,
            width=width,
            height=height,
            fps=fps,
            video_bitrate=video_bitrate,
            audio_bitrate=audio_bitrate,
        )

    @staticmethod
    def calculate_optimal_params(
        video_info: VideoInfo,
        target_bytes: int,
        audio_bitrate_kbps: int = 16,
        fixed_fps: Optional[float] = None,
    ) -> Tuple[int, int, float, int]:
        """
        Calculate optimal encoding parameters for target file size.

        Returns: (width, height, fps, video_bitrate_kbps)
        """
        usable_bytes = int(target_bytes * 0.98)

        total_bitrate_kbps = (usable_bytes * 8) / (video_info.duration * 1000)
        video_bitrate_kbps = total_bitrate_kbps - audio_bitrate_kbps

        effective_fps = fixed_fps if fixed_fps is not None else video_info.fps
        fps_factor = effective_fps / 30.0
        min_video_bitrate = max(50 * fps_factor, 20)

        # if video_bitrate_kbps < min_video_bitrate:
        #     raise ValueError(
        #         f"Target size too small. Need at least "
        #         f"{int((min_video_bitrate + audio_bitrate_kbps) * video_info.duration * 1000 / 8 / 1024)}KB "
        #         f"for a {video_info.duration:.1f}s video at {effective_fps:.1f}fps"
        #     )

        resolution_tiers = [
            (1920, 1080, 1500, 4000),  # 1080p
            (1280, 720, 800, 2500),    # 720p
            (854, 480, 400, 1200),     # 480p
            (640, 360, 200, 700),      # 360p
            (426, 240, 100, 400),      # 240p
            (320, 180, 50, 200),       # 180p
        ]

        fps_tiers = [60, 30, 24, 15, 10]
        aspect_ratio = video_info.width / video_info.height

        best_width, best_height = resolution_tiers[-1][0], resolution_tiers[-1][1]

        for width, height, min_br, ideal_br in resolution_tiers:
            if aspect_ratio > width / height:
                adjusted_height = int(width / aspect_ratio)
                adjusted_height = adjusted_height - (adjusted_height % 2)
                adjusted_width = width
            else:
                adjusted_width = int(height * aspect_ratio)
                adjusted_width = adjusted_width - (adjusted_width % 2)
                adjusted_height = height

            if adjusted_width > video_info.width or adjusted_height > video_info.height:
                adjusted_width = video_info.width - (video_info.width % 2)
                adjusted_height = video_info.height - (video_info.height % 2)

            pixel_ratio = (adjusted_width * adjusted_height) / (width * height)
            scaled_min_br = min_br * pixel_ratio

            if video_bitrate_kbps >= scaled_min_br:
                best_width, best_height = adjusted_width, adjusted_height
                break

        if fixed_fps is not None:
            best_fps = fixed_fps
        else:
            best_fps = fps_tiers[-1]

            for fps in fps_tiers:
                if fps > video_info.fps:
                    continue

                fps_factor = fps / 30.0
                effective_bitrate = video_bitrate_kbps / max(fps_factor, 0.5)
                pixels_per_sec = best_width * best_height * fps
                bpp = (video_bitrate_kbps * 1000) / pixels_per_sec

                if bpp >= 0.03:
                    best_fps = fps
                    break

        video_bitrate_kbps = int(video_bitrate_kbps)

        return best_width, best_height, best_fps, video_bitrate_kbps

    def encode_video(
        self,
        input_file: str,
        output_file: str,
        width: int,
        height: int,
        fps: float,
        video_bitrate_kbps: int,
        audio_bitrate_kbps: int = 16,
        two_pass: bool = True,
        target_bytes: Optional[int] = None,
        size_tolerance: float = 0.05,
    ) -> bool:
        """
        Encode video with specified parameters using libx265 (CPU).

        If target_bytes is provided, will do an additional calibration pass
        to adjust for bitrate inaccuracy.

        Returns True if successful.
        """
        os.makedirs(self.temp_dir, exist_ok=True)
        run_temp_dir = tempfile.mkdtemp(prefix="compress-", dir=self.temp_dir)

        def run_encode(
            bitrate_kbps: int,
            output_path: str,
            pass_name: str = "Encoding",
        ) -> bool:
            """Run the actual encode with given bitrate."""
            bufsize_kbps = max(bitrate_kbps, 300)
            vf_string = f"scale={width}:{height}:flags=lanczos,fps={fps}"

            passlog = os.path.join(run_temp_dir, "ffmpeg2pass").replace("\\", "/")

            base_args = [
                self.ffmpeg_path,
                "-y",
                "-i", input_file,
                "-vf", vf_string,
                "-c:v", "libx265",
                "-preset", "slow",
                "-profile:v", "main",
                "-pix_fmt", "yuv420p",
                "-tag:v", "hvc1",
                "-c:a", "aac",
                "-ac", "1",
                "-ar", "22050",
                "-b:a", f"{audio_bitrate_kbps}k",
                "-movflags", "+faststart",
            ]

            if two_pass:
                self.log(f"{pass_name}: Running analysis pass...")
                pass1_args = base_args + [
                    "-b:v", f"{bitrate_kbps}k",
                    "-maxrate", f"{int(bitrate_kbps * 1.5)}k",
                    "-bufsize", f"{bufsize_kbps}k",
                    "-x265-params", f"pass=1:stats={passlog}:aq-mode=3:aq-strength=1.0:psy-rd=2.0:psy-rdoq=1.0:rc-lookahead=32:bframes=4:b-adapt=2:subme=7:deblock=-1,-1",
                    "-an",
                    "-f", "null",
                    "/dev/null" if os.name != "nt" else "NUL",
                ]

                result = subprocess.run(pass1_args, capture_output=True, text=True)
                if result.returncode != 0:
                    self.log(f"Analysis pass failed: {result.stderr}")
                    return False

                self.log(f"{pass_name}: Running encode pass...")
                pass2_args = base_args + [
                    "-b:v", f"{bitrate_kbps}k",
                    "-maxrate", f"{int(bitrate_kbps * 1.5)}k",
                    "-bufsize", f"{bufsize_kbps}k",
                    "-x265-params", f"pass=2:stats={passlog}:aq-mode=3:aq-strength=1.0:psy-rd=2.0:psy-rdoq=1.0:rc-lookahead=32:bframes=4:b-adapt=2:subme=7:deblock=-1,-1",
                    output_path,
                ]

                result = subprocess.run(pass2_args, capture_output=True, text=True)
                if result.returncode != 0:
                    self.log(f"Encode pass failed: {result.stderr}")
                    return False
            else:
                self.log(f"{pass_name}...")
                single_args = base_args + [
                    "-b:v", f"{bitrate_kbps}k",
                    "-maxrate", f"{int(bitrate_kbps * 1.5)}k",
                    "-bufsize", f"{bufsize_kbps}k",
                    "-x265-params", "aq-mode=3:aq-strength=1.0:psy-rd=2.0:psy-rdoq=1.0:rc-lookahead=32:bframes=4:b-adapt=2:subme=7:deblock=-1,-1",
                    output_path,
                ]

                result = subprocess.run(single_args, capture_output=True, text=True)
                if result.returncode != 0:
                    self.log(f"Encoding failed: {result.stderr}")
                    return False

            return True

        try:
            if target_bytes is None:
                return run_encode(video_bitrate_kbps, output_file, "Encoding video")

            temp_output = os.path.join(run_temp_dir, "calibration_encode.mp4")

            self.log("Pass 1/2: Calibration encode...")
            if not run_encode(video_bitrate_kbps, temp_output, "Calibration"):
                return False

            actual_size = os.path.getsize(temp_output)
            size_ratio = target_bytes / actual_size
            size_diff = abs(1.0 - size_ratio)

            self.log(f"  Calibration result: {actual_size / 1024:.1f} KB (target: {target_bytes / 1024:.1f} KB)")
            self.log(f"  Ratio: {size_ratio:.3f} (diff: {size_diff * 100:.1f}%)")

            if size_diff <= size_tolerance:
                self.log(f"  Within {size_tolerance * 100:.0f}% tolerance, using calibration encode.")
                shutil.move(temp_output, output_file)
                return True

            adjusted_bitrate = int(video_bitrate_kbps * size_ratio)
            self.log(f"  Adjusting bitrate: {video_bitrate_kbps} -> {adjusted_bitrate} kbps")

            self.log("Pass 2/2: Final encode with adjusted bitrate...")
            return run_encode(adjusted_bitrate, output_file, "Final encode")
        finally:
            shutil.rmtree(run_temp_dir, ignore_errors=True)

    @staticmethod
    def compressed_output_path(input_file: str) -> str:
        """Return the default compressed output path for an input file."""
        input_path = Path(input_file)
        if input_path.suffix:
            return str(input_path.with_name(f"{input_path.stem}-compressed{input_path.suffix}"))
        return str(input_path.with_name(f"{input_path.name}-compressed"))

    def compress_file(
        self,
        input_file: str,
        output_file: Optional[str] = None,
        target_size_kb: float = DEFAULT_TARGET_SIZE_KB,
        fps: float = DEFAULT_FPS,
        audio_bitrate_kbps: int = DEFAULT_AUDIO_BITRATE_KBPS,
        two_pass: bool = True,
        invert_aspect_ratio: bool = False,
    ) -> str:
        """Compress one input file and return the output path."""
        if output_file is None:
            output_file = self.compressed_output_path(input_file)

        target_bytes = int(target_size_kb * 1024)
        self.log(f"Target file size: {self.format_size(target_bytes)}")
        self.log(f"Analyzing input video: {input_file}")

        video_info = self.get_video_info(input_file, invert_aspect_ratio)
        self.log(f"  Duration: {video_info.duration:.2f}s")
        self.log(f"  Resolution: {video_info.width}x{video_info.height}")
        self.log(f"  FPS: {video_info.fps:.2f}")

        if fps > video_info.fps:
            self.log(f"  Warning: Requested FPS ({fps}) exceeds source FPS ({video_info.fps:.2f})")

        width, height, output_fps, video_bitrate = self.calculate_optimal_params(
            video_info,
            target_bytes,
            audio_bitrate_kbps,
            fixed_fps=fps,
        )

        self.log("Optimal encoding parameters:")
        self.log(f"  Resolution: {width}x{height}")
        self.log(f"  FPS: {output_fps}")
        self.log(f"  Video bitrate: {video_bitrate} kbps")
        self.log(f"  Audio bitrate: {audio_bitrate_kbps} kbps")

        expected_size = int((video_bitrate + audio_bitrate_kbps) * video_info.duration * 1000 / 8)
        self.log(f"  Expected size: ~{self.format_size(expected_size)}")
        self.log(f"Encoding to: {output_file}")

        success = self.encode_video(
            input_file,
            output_file,
            width,
            height,
            output_fps,
            video_bitrate,
            audio_bitrate_kbps,
            two_pass=two_pass,
            target_bytes=target_bytes,
        )

        if not success:
            raise RuntimeError("Video encoding failed")

        actual_size = os.path.getsize(output_file)
        self.log("Encoding complete")
        self.log(f"  Target size: {self.format_size(target_bytes)}")
        self.log(f"  Actual size: {self.format_size(actual_size)}")
        self.log(f"  Difference: {(actual_size - target_bytes) / target_bytes * 100:+.1f}%")

        return output_file

    @staticmethod
    def format_size(bytes_val: int) -> str:
        """Format bytes as human-readable string."""
        for unit in ["B", "KB", "MB", "GB"]:
            if bytes_val < 1024:
                return f"{bytes_val:.2f} {unit}"
            bytes_val /= 1024
        return f"{bytes_val:.2f} TB"


_DEFAULT_COMPRESSOR = VideoCompressor()


def parse_size(size_str: str) -> int:
    """Parse a size string like '10MB' or '500KB' into bytes."""
    return _DEFAULT_COMPRESSOR.parse_size(size_str)


def get_video_info(input_file: str, invert_aspect_ratio: bool = False) -> VideoInfo:
    """Extract video information using ffprobe."""
    return _DEFAULT_COMPRESSOR.get_video_info(input_file, invert_aspect_ratio)


def calculate_optimal_params(
    video_info: VideoInfo,
    target_bytes: int,
    audio_bitrate_kbps: int = 16,
    fixed_fps: Optional[float] = None,
) -> Tuple[int, int, float, int]:
    """Calculate optimal encoding parameters for target file size."""
    return _DEFAULT_COMPRESSOR.calculate_optimal_params(
        video_info,
        target_bytes,
        audio_bitrate_kbps,
        fixed_fps,
    )


def encode_video(
    input_file: str,
    output_file: str,
    width: int,
    height: int,
    fps: float,
    video_bitrate_kbps: int,
    audio_bitrate_kbps: int = 16,
    two_pass: bool = True,
    target_bytes: Optional[int] = None,
    size_tolerance: float = 0.05,
) -> bool:
    """Encode video with specified parameters using libx265 (CPU)."""
    return _DEFAULT_COMPRESSOR.encode_video(
        input_file,
        output_file,
        width,
        height,
        fps,
        video_bitrate_kbps,
        audio_bitrate_kbps,
        two_pass,
        target_bytes,
        size_tolerance,
    )


def format_size(bytes_val: int) -> str:
    """Format bytes as human-readable string."""
    return _DEFAULT_COMPRESSOR.format_size(bytes_val)


def compressed_output_path(input_file: str) -> str:
    """Return the default compressed output path for an input file."""
    return _DEFAULT_COMPRESSOR.compressed_output_path(input_file)


def compress_file(
    input_file: str,
    output_file: Optional[str] = None,
    target_size_kb: float = VideoCompressor.DEFAULT_TARGET_SIZE_KB,
    fps: float = VideoCompressor.DEFAULT_FPS,
    audio_bitrate_kbps: int = VideoCompressor.DEFAULT_AUDIO_BITRATE_KBPS,
    two_pass: bool = True,
    invert_aspect_ratio: bool = False,
) -> str:
    """Compress one input file and return the output path."""
    return _DEFAULT_COMPRESSOR.compress_file(
        input_file,
        output_file,
        target_size_kb,
        fps,
        audio_bitrate_kbps,
        two_pass,
        invert_aspect_ratio,
    )


def configure_logging(
    log_level: str,
    log_dir: str = DEFAULT_LOG_DIR,
    log_file: str = DEFAULT_LOG_FILE,
) -> Path:
    """Configure process logging for the RabbitMQ worker CLI."""
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {log_level}")

    log_path = Path(log_file)
    if not log_path.is_absolute():
        log_path = Path(log_dir) / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    console_handler = logging.StreamHandler()
    file_handler = RotatingFileHandler(
        log_path,
        mode="a",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )

    logging.basicConfig(
        level=numeric_level,
        format=LOG_FORMAT,
        handlers=[console_handler, file_handler],
        force=True,
    )
    LOGGER.info("Logging to %s", log_path)
    return log_path


class RabbitMQVideoCompressionAPI:
    """RabbitMQ consumer that compresses videos from filename messages."""

    def __init__(
        self,
        config: RabbitMQConfig,
        compressor: Optional[VideoCompressor] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(f"{__name__}.RabbitMQVideoCompressionAPI")
        self.compressor = compressor or VideoCompressor()
        self.executor = ThreadPoolExecutor(max_workers=self.config.max_concurrent_jobs)
        self.connection = None
        self.channel = None

    @staticmethod
    def parse_file_name_message(body: bytes) -> str:
        """Parse a RabbitMQ message body into an input filename."""
        text = body.decode("utf-8").strip()
        if not text:
            raise ValueError("Message body is empty")

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text

        if isinstance(payload, str):
            file_name = payload
        elif isinstance(payload, dict):
            file_name = (
                payload.get("file_name")
                or payload.get("filename")
                or payload.get("file")
                or payload.get("path")
            )
        else:
            file_name = None

        if not file_name:
            raise ValueError(
                "Message must be a filename string or JSON with file_name, filename, file, or path"
            )

        return str(file_name).strip()

    def connect(self):
        """Open a RabbitMQ connection and channel."""
        try:
            import pika
        except ImportError as exc:
            raise RuntimeError("RabbitMQ support requires pika. Install it with: py -3 -m pip install pika") from exc

        credentials = pika.PlainCredentials(self.config.username, self.config.password)
        parameters = pika.ConnectionParameters(
            host=self.config.host,
            port=self.config.port,
            virtual_host=self.config.virtual_host,
            credentials=credentials,
            heartbeat=self.config.heartbeat,
            blocked_connection_timeout=self.config.blocked_connection_timeout,
        )

        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        channel.queue_declare(queue=self.config.queue_name, durable=True)
        channel.basic_qos(prefetch_count=self.config.prefetch_count)
        return connection, channel

    def process_message(self, channel, delivery_tag: int, body: bytes) -> None:
        """Compress one RabbitMQ message in a worker thread."""
        try:
            input_file = self.parse_file_name_message(body)
            output_file = self.compressor.compressed_output_path(input_file)

            self.logger.info("Received compression request: %s", input_file)
            self.logger.info("Output file: %s", output_file)

            self.compressor.compress_file(
                input_file=input_file,
                output_file=output_file,
                target_size_kb=VideoCompressor.DEFAULT_TARGET_SIZE_KB,
                fps=VideoCompressor.DEFAULT_FPS,
                audio_bitrate_kbps=VideoCompressor.DEFAULT_AUDIO_BITRATE_KBPS,
            )

            self.ack_message(channel, delivery_tag)
            self.logger.info("Compression finished: %s", output_file)
        except Exception as exc:
            self.logger.exception("Compression request failed: %s", exc)
            self.nack_message(channel, delivery_tag)

    def ack_message(self, channel, delivery_tag: int) -> None:
        """Schedule a RabbitMQ ack on the connection thread."""
        if self.connection and self.connection.is_open:
            def do_ack() -> None:
                if channel.is_open:
                    channel.basic_ack(delivery_tag=delivery_tag)

            try:
                self.connection.add_callback_threadsafe(do_ack)
            except Exception as exc:
                self.logger.exception("Could not schedule RabbitMQ ack: %s", exc)

    def nack_message(self, channel, delivery_tag: int) -> None:
        """Schedule a RabbitMQ nack on the connection thread."""
        if self.connection and self.connection.is_open:
            def do_nack() -> None:
                if channel.is_open:
                    channel.basic_nack(
                        delivery_tag=delivery_tag,
                        requeue=self.config.requeue_on_failure,
                    )

            try:
                self.connection.add_callback_threadsafe(do_nack)
            except Exception as exc:
                self.logger.exception("Could not schedule RabbitMQ nack: %s", exc)

    def on_message(self, channel, method, properties, body: bytes) -> None:
        """Queue one RabbitMQ message for background compression."""
        self.executor.submit(self.process_message, channel, method.delivery_tag, body)

    def start(self) -> None:
        """Start consuming compression requests from RabbitMQ."""
        connection, channel = self.connect()
        self.connection = connection
        self.channel = channel
        channel.basic_consume(
            queue=self.config.queue_name,
            on_message_callback=self.on_message,
            auto_ack=False,
        )

        self.logger.info("Waiting for video compression messages on queue: %s", self.config.queue_name)
        self.logger.info("Max concurrent compression jobs: %s", self.config.max_concurrent_jobs)
        self.logger.info("Messages may be plain filenames or JSON with a file_name field.")

        try:
            channel.start_consuming()
        except KeyboardInterrupt:
            self.logger.info("Stopping video compression API")
            channel.stop_consuming()
        finally:
            self.executor.shutdown(wait=True)
            if connection.is_open:
                connection.close()


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI arguments for the RabbitMQ API worker."""
    defaults = RabbitMQConfig.from_env()
    parser = argparse.ArgumentParser(description="RabbitMQ video compression API worker")
    parser.add_argument("--rabbit-host", default=defaults.host)
    parser.add_argument("--rabbit-port", type=int, default=defaults.port)
    parser.add_argument("--rabbit-vhost", default=defaults.virtual_host)
    parser.add_argument("--rabbit-user", default=defaults.username)
    parser.add_argument("--rabbit-pass", default=defaults.password)
    parser.add_argument("--rabbit-heartbeat", type=int, default=defaults.heartbeat)
    parser.add_argument(
        "--rabbit-blocked-connection-timeout",
        type=int,
        default=defaults.blocked_connection_timeout,
    )
    parser.add_argument("--queue", default=defaults.queue_name)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=defaults.max_concurrent_jobs,
        help="Maximum number of videos to compress at the same time",
    )
    parser.add_argument(
        "--prefetch",
        type=int,
        default=int(os.getenv("RABBITMQ_PREFETCH_COUNT")) if os.getenv("RABBITMQ_PREFETCH_COUNT") else None,
        help="RabbitMQ unacked message limit. Defaults to --concurrency.",
    )
    parser.add_argument(
        "--requeue-on-failure",
        action="store_true",
        default=defaults.requeue_on_failure,
        help="Requeue failed messages instead of rejecting them",
    )
    parser.add_argument("--ffmpeg-path", default=os.getenv("FFMPEG_PATH", "ffmpeg"))
    parser.add_argument("--ffprobe-path", default=os.getenv("FFPROBE_PATH", "ffprobe"))
    parser.add_argument("--temp-dir", default=os.getenv("VIDEO_COMPRESS_TEMP_DIR", "tmp"))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    parser.add_argument("--log-dir", default=os.getenv("LOG_DIR", DEFAULT_LOG_DIR))
    parser.add_argument("--log-file", default=os.getenv("LOG_FILE", DEFAULT_LOG_FILE))
    return parser


def main() -> None:
    """Run the RabbitMQ video compression API."""
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")

    if args.prefetch is not None and args.prefetch < 1:
        parser.error("--prefetch must be at least 1")

    try:
        configure_logging(args.log_level, args.log_dir, args.log_file)
    except ValueError as exc:
        parser.error(str(exc))

    prefetch_count = args.prefetch if args.prefetch is not None else args.concurrency

    config = RabbitMQConfig(
        host=args.rabbit_host,
        port=args.rabbit_port,
        virtual_host=args.rabbit_vhost,
        username=args.rabbit_user,
        password=args.rabbit_pass,
        heartbeat=args.rabbit_heartbeat,
        blocked_connection_timeout=args.rabbit_blocked_connection_timeout,
        queue_name=args.queue,
        max_concurrent_jobs=args.concurrency,
        prefetch_count=prefetch_count,
        requeue_on_failure=args.requeue_on_failure,
    )
    compressor = VideoCompressor(
        ffmpeg_path=args.ffmpeg_path,
        ffprobe_path=args.ffprobe_path,
        temp_dir=args.temp_dir,
        logger=LOGGER.getChild("compressor"),
    )
    api = RabbitMQVideoCompressionAPI(
        config=config,
        compressor=compressor,
        logger=LOGGER.getChild("rabbitmq"),
    )
    api.start()


if __name__ == "__main__":
    main()
