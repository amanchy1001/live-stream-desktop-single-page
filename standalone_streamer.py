#!/usr/bin/env python3
"""
Standalone RTSP Live Stream Desktop App
Auto-connects to production server, handles internet disconnections, and manages streams automatically.
"""

import json
import logging
import subprocess
import sys
import time
import threading
import atexit
import signal
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import requests
import socket
import os

# Configuration - HARDCODED VALUES
PRODUCTION_SERVER_URL = "http://172.105.52.55:8011"  # CHANGE THIS TO YOUR PRODUCTION URL
RTMP_SERVER_URL = "rtmp://172.105.52.55:1935/live"  # RTMP server for forwarding streams
CAMERAS_JSON_FILE = "cameras.json"  # JSON file in same directory as exe
LOG_FILE = "stream_log.txt"

# Camera Configuration
CAMERAS_CONFIG_FILE = "cameras.json"

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class Camera:
    """Camera configuration"""
    def __init__(self, camera_id: str, name: str, rtsp_url: str):
        self.id = camera_id
        self.name = name
        self.rtsp_url = rtsp_url

class RTSPForwarder:
    """Handles RTSP capture and RTMP forwarding from desktop app to backend"""

    def __init__(self):
        self.ffmpeg_path = "ffmpeg"
        self.active_processes: Dict[str, subprocess.Popen] = {}
        self.stream_info = {}

    def build_rtmp_forward_command(self, stream_id: str, rtsp_url: str) -> list:
        """Build FFmpeg command to capture RTSP and forward via RTMP"""
        # For local testing (when backend is localhost), we'll use direct HLS output
        # For remote backend, we'll use RTMP forwarding
        USE_LOCAL_MODE = PRODUCTION_SERVER_URL.startswith("http://localhost") or PRODUCTION_SERVER_URL.startswith("http://127.0.0.1")

        if USE_LOCAL_MODE:
            # For local testing: Save RTSP as HLS files directly
            # Backend will serve these files
            output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend", "streams")
            hls_output = os.path.join(output_dir, stream_id, "playlist.m3u8")

            cmd = [
                self.ffmpeg_path,
                "-rtsp_transport", "tcp",  # Use TCP for RTSP connection
                "-i", rtsp_url,  # RTSP input from camera
                "-c:v", "libx264",  # H.264 video codec
                "-preset", "ultrafast",  # Fast encoding preset
                "-b:v", "2500k",  # Video bitrate
                "-maxrate", "3000k",  # Max bitrate
                "-bufsize", "6000k",  # Buffer size
                "-c:a", "aac",  # AAC audio codec
                "-b:a", "128k",  # Audio bitrate
                "-f", "hls",  # HLS output format
                "-hls_time", "10",  # Segment duration
                "-hls_list_size", "3",  # Keep 3 segments
                "-hls_flags", "delete_segments",  # Delete old segments
                "-loglevel", "error",  # Reduce FFmpeg logging
                hls_output  # HLS output file
            ]
        else:
            # For remote backend: Forward via RTMP
            rtmp_output_url = f"{RTMP_SERVER_URL.rstrip('/')}/{stream_id}"

            cmd = [
                self.ffmpeg_path,
                "-rtsp_transport", "tcp",  # Use TCP for RTSP connection
                "-i", rtsp_url,  # RTSP input from camera
                "-c:v", "libx264",  # H.264 video codec
                "-preset", "ultrafast",  # Fast encoding preset
                "-b:v", "2500k",  # Video bitrate
                "-maxrate", "3000k",  # Max bitrate
                "-bufsize", "6000k",  # Buffer size
                "-pix_fmt", "yuv420p",  # Pixel format for RTMP compatibility
                "-g", "50",  # Keyframe interval
                "-c:a", "aac",  # AAC audio codec
                "-b:a", "128k",  # Audio bitrate
                "-ar", "44100",  # Audio sample rate
                "-ac", "2",  # Stereo audio
                "-f", "flv",  # FLV format for RTMP
                "-flvflags", "no_duration_filesize",  # FLV optimizations
                "-loglevel", "error",  # Reduce FFmpeg logging
                rtmp_output_url  # RTMP output URL
            ]

        return cmd

    def start_forwarding(self, stream_id: str, rtsp_url: str) -> bool:
        """Start RTSP to RTMP forwarding for a specific camera"""
        try:
            # Check if already forwarding this stream
            if stream_id in self.active_processes:
                logger.warning(f"RTMP forwarding already active for {stream_id}")
                return True

            # Build FFmpeg command
            cmd = self.build_rtmp_forward_command(stream_id, rtsp_url)

            logger.info("="*60)
            logger.info(f"🎬 STARTING RTSP FORWARDING - Stream: {stream_id}")
            logger.info(f"📡 RTSP Source: {rtsp_url}")
            logger.info(f"🌐 RTMP Destination: {cmd[-1]}")
            logger.info(f"💻 FFmpeg Command: {' '.join(cmd)}")
            logger.info("="*60)

            # Start FFmpeg process
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            )

            self.active_processes[stream_id] = process
            self.stream_info[stream_id] = {
                'rtsp_url': rtsp_url,
                'start_time': time.time(),
                'process': process
            }

            logger.info(f"✅ RTMP forwarding PROCESS STARTED for {stream_id} (PID: {process.pid})")

            # Log for first 10 seconds to catch initialization issues
            import threading
            def log_process_status():
                try:
                    for i in range(10):
                        if process.poll() is None:  # Still running
                            time.sleep(1)
                            if i in [1, 3, 5, 9]:  # Log at specific intervals
                                logger.info(f"🔄 {stream_id} still running after {i+1}s (PID: {process.pid})")
                        else:
                            # Process ended early
                            return_code = process.poll()
                            stdout, stderr = process.communicate()
                            logger.error(f"❌ {stream_id} PROCESS ENDED EARLY (code: {return_code})")
                            logger.error(f"   Stdout: {stdout.decode()[:200]}")
                            logger.error(f"   Stderr: {stderr.decode()[:200]}")
                            break
                except Exception as e:
                    logger.error(f"Error monitoring process {stream_id}: {e}")

            # Start monitoring thread
            monitor_thread = threading.Thread(target=log_process_status, daemon=True)
            monitor_thread.start()

            return True

        except Exception as e:
            logger.error(f"❌ FAILED TO START RTMP forwarding for {stream_id}: {str(e)}")
            logger.error("Full exception:", exc_info=True)
            return False

    def stop_forwarding(self, stream_id: str) -> bool:
        """Stop RTSP to RTMP forwarding for a specific camera"""
        try:
            if stream_id not in self.active_processes:
                logger.warning(f"No active RTMP forwarding for {stream_id}")
                return True

            process = self.active_processes[stream_id]

            # Terminate the process gracefully first
            process.terminate()
            try:
                process.wait(timeout=5)  # Wait up to 5 seconds
            except subprocess.TimeoutExpired:
                # Force kill if graceful termination fails
                logger.warning(f"Force killing RTMP forwarding process for {stream_id}")
                process.kill()
                process.wait()

            # Cleanup
            del self.active_processes[stream_id]
            del self.stream_info[stream_id]

            logger.info(f"RTMP forwarding stopped for {stream_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to stop RTMP forwarding for {stream_id}: {str(e)}")
            return False

    def is_forwarding(self, stream_id: str) -> bool:
        """Check if RTMP forwarding is active for a stream"""
        if stream_id not in self.active_processes:
            return False

        process = self.active_processes[stream_id]

        # Check if process is still running
        if process.poll() is not None:
            # Process has terminated
            logger.warning(f"RTMP forwarding process for {stream_id} has terminated unexpectedly")
            # Cleanup
            del self.active_processes[stream_id]
            del self.stream_info[stream_id]
            return False

        return True

    def get_all_active_forwarding(self) -> Dict[str, dict]:
        """Get status of all active RTMP forwarding processes"""
        active_streams = {}

        # Check each stream
        for stream_id in list(self.active_processes.keys()):
            if self.is_forwarding(stream_id):
                active_streams[stream_id] = {
                    'rtsp_url': self.stream_info[stream_id]['rtsp_url'],
                    'start_time': self.stream_info[stream_id]['start_time'],
                    'running_time': time.time() - self.stream_info[stream_id]['start_time']
                }

        return active_streams

    def stop_all_forwarding(self):
        """Stop all active RTMP forwarding processes"""
        for stream_id in list(self.active_processes.keys()):
            self.stop_forwarding(stream_id)
        logger.info("All RTMP forwarding processes stopped")

    def health_check(self) -> bool:
        """Check if RTMP forwarder is healthy (can execute FFmpeg)"""
        try:
            # Quick test to see if FFmpeg is available
            result = subprocess.run(
                [self.ffmpeg_path, "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"RTMP forwarder health check failed: {str(e)}")
            return False

class StreamManager:
    """Manages RTSP to RTMP forwarding and backend communication"""

    def __init__(self):
        self.server_url = PRODUCTION_SERVER_URL.rstrip('/')
        self.session = requests.Session()
        self.session.timeout = 30
        self.active_streams: Dict[str, Camera] = {}
        self.rtsp_forwarder = RTSPForwarder()
        self.internet_connected = True
        self.monitoring_active = False

    def load_cameras(self) -> List[Camera]:
        """Load cameras from JSON file"""
        try:
            if not Path(CAMERAS_JSON_FILE).exists():
                logger.error(f"Cameras JSON file not found: {CAMERAS_JSON_FILE}")
                return []

            with open(CAMERAS_JSON_FILE, 'r') as f:
                data = json.load(f)

            cameras = []
            for cam_data in data.get('cameras', []):
                camera = Camera(
                    camera_id=cam_data['id'],
                    name=cam_data['name'],
                    rtsp_url=cam_data['rtsp_url']
                )
                cameras.append(camera)

            logger.info(f"Loaded {len(cameras)} cameras from {CAMERAS_JSON_FILE}")
            return cameras

        except Exception as e:
            logger.error(f"Error loading cameras: {str(e)}")
            return []

    def check_internet_connection(self) -> bool:
        """Check if internet connection is available"""
        try:
            # Try to connect to a reliable host
            socket.create_connection(("8.8.8.8", 53), timeout=5)
            return True
        except OSError:
            return False

    def check_server_connection(self) -> bool:
        """Check if production server is reachable"""
        try:
            response = self.session.get(f"{self.server_url}/", timeout=10)
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Server connection check failed: {str(e)}")
            return False

    def start_stream(self, camera: Camera) -> bool:
        """Start RTSP to RTMP forwarding for a camera"""
        try:
            if camera.id in self.active_streams:
                logger.info(f"Stream already active for {camera.name}")
                return True

            logger.info(f"Starting RTSP forwarding for {camera.name} ({camera.id})")

            # Start RTSP to RTMP forwarding
            forwarding_started = self.rtsp_forwarder.start_forwarding(camera.id, camera.rtsp_url)

            if not forwarding_started:
                logger.error(f"Failed to start RTMP forwarding for {camera.name}")
                return False

            self.active_streams[camera.id] = camera

            # Notify server about the stream
            self.notify_server_stream_started(camera)

            logger.info(f"RTMP forwarding started successfully for {camera.name}")
            return True

        except Exception as e:
            logger.error(f"Failed to start stream for {camera.name}: {str(e)}")
            return False

    def stop_stream(self, camera_id: str, reason: str = "Manual stop") -> bool:
        """Stop RTMP forwarding for a camera"""
        try:
            if camera_id not in self.active_streams:
                return True

            camera = self.active_streams[camera_id]
            logger.info(f"Stopping RTMP forwarding for {camera.name} - Reason: {reason}")

            # Stop RTMP forwarding
            forwarding_stopped = self.rtsp_forwarder.stop_forwarding(camera_id)

            if not forwarding_stopped:
                logger.warning(f"Failed to stop RTMP forwarding for {camera.name}")

            # Notify server
            self.notify_server_stream_stopped(camera, reason)

            del self.active_streams[camera_id]

            logger.info(f"RTMP forwarding stopped for {camera.name}")
            return True

        except Exception as e:
            logger.error(f"Failed to stop stream for {camera_id}: {str(e)}")
            return False

    def notify_server_stream_started(self, camera: Camera):
        """Notify server that stream has started"""
        try:
            payload = {
                "stream_id": camera.id,
                "rtsp_url": camera.rtsp_url
            }
            response = self.session.post(
                f"{self.server_url}/api/streams/start",
                json=payload,
                timeout=10
            )
            if response.status_code == 200:
                logger.info(f"Server notified: Stream started for {camera.name}")
            else:
                logger.warning(f"Server notification failed for {camera.name}: {response.status_code}")
        except Exception as e:
            logger.warning(f"Could not notify server about stream start for {camera.name}: {str(e)}")

    def notify_server_stream_stopped(self, camera: Camera, reason: str):
        """Notify server that stream has stopped"""
        try:
            payload = {"stream_id": camera.id}
            response = self.session.post(
                f"{self.server_url}/api/streams/stop",
                json=payload,
                timeout=10
            )
            if response.status_code == 200:
                logger.info(f"Server notified: Stream stopped for {camera.name} - {reason}")
            else:
                logger.warning(f"Server notification failed for {camera.name}: {response.status_code}")
        except Exception as e:
            logger.warning(f"Could not notify server about stream stop for {camera.name}: {str(e)}")

    def restart_failed_stream(self, camera_id: str) -> bool:
        """Restart a failed stream"""
        try:
            if camera_id not in self.active_streams:
                logger.warning(f"Cannot restart {camera_id}: not in active streams")
                return False

            camera = self.active_streams[camera_id]
            logger.info(f"Restarting failed stream for {camera.name}")

            # Stop current stream
            self.stop_stream(camera_id, "Restarting failed stream")

            # Wait a moment
            time.sleep(2)

            # Start again
            return self.start_stream(camera)

        except Exception as e:
            logger.error(f"Failed to restart stream for {camera_id}: {str(e)}")
            return False

    def check_stream_health(self, camera_id: str) -> bool:
        """Check if RTMP forwarding is still healthy"""
        return self.rtsp_forwarder.is_forwarding(camera_id)

    def monitor_streams(self):
        """Monitor all active streams and restart failed ones"""
        logger.info("Starting stream monitoring...")

        while self.monitoring_active:
            try:
                # Check internet connection
                current_internet_status = self.check_internet_connection()

                if current_internet_status != self.internet_connected:
                    if current_internet_status:
                        logger.info("Internet connection restored")
                        self.internet_connected = True
                        # Restart all streams when internet comes back
                        cameras = self.load_cameras()
                        for camera in cameras:
                            if camera.id not in self.active_streams:
                                self.start_stream(camera)
                    else:
                        logger.warning("Internet connection lost")
                        self.internet_connected = False

                # Check server connection if internet is available
                server_connected = self.check_server_connection() if self.internet_connected else False

                # Check each active stream
                failed_streams = []
                for camera_id in list(self.active_streams.keys()):
                    if not self.check_stream_health(camera_id):
                        failed_streams.append(camera_id)
                        camera = self.active_streams[camera_id]
                        logger.warning(f"Stream failed for {camera.name} - FFmpeg process terminated")

                # Restart failed streams
                for camera_id in failed_streams:
                    if self.internet_connected:
                        success = self.restart_failed_stream(camera_id)
                        if success:
                            logger.info(f"Successfully restarted stream for {camera_id}")
                        else:
                            logger.error(f"Failed to restart stream for {camera_id}")
                    else:
                        logger.info(f"Skipping restart for {camera_id} - no internet connection")

                # Log status every 5 minutes
                logger.info(f"Monitoring status: Internet={self.internet_connected}, Server={server_connected}, Active streams={len(self.active_streams)}")

            except Exception as e:
                logger.error(f"Error in stream monitoring: {str(e)}")

            time.sleep(30)  # Check every 30 seconds

    def start_monitoring(self):
        """Start the monitoring thread"""
        self.monitoring_active = True
        monitor_thread = threading.Thread(target=self.monitor_streams, daemon=True)
        monitor_thread.start()
        logger.info("Stream monitoring started")

    def stop_monitoring(self):
        """Stop the monitoring thread"""
        self.monitoring_active = False
        logger.info("Stream monitoring stopped")

    def start_all_streams(self):
        """Start streaming for all cameras"""
        cameras = self.load_cameras()
        if not cameras:
            logger.error("No cameras configured")
            return

        logger.info(f"Starting streams for {len(cameras)} cameras...")

        for camera in cameras:
            self.start_stream(camera)

    def stop_all_streams(self, reason: str = "Application shutdown"):
        """Stop all active RTMP forwarding processes"""
        logger.info(f"Stopping all RTMP forwarding - {reason}")

        # Stop all RTMP forwarding processes
        self.rtsp_forwarder.stop_all_forwarding()

        # Clear active streams
        self.active_streams.clear()

        logger.info("All RTMP forwarding stopped")

def cleanup_handler():
    """Handle cleanup when application exits"""
    logger.info("Application exit detected - performing cleanup...")
    try:
        # This will be called when the application exits
        # We need to access the global manager instance
        if 'manager' in globals():
            manager.stop_monitoring()
            manager.stop_all_streams("Application exit")
            logger.info("Cleanup completed successfully")
    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}")

def signal_handler(signum, frame):
    """Handle system signals"""
    logger.info(f"Received signal {signum} - shutting down gracefully...")
    cleanup_handler()
    sys.exit(0)

def main():
    """Main application"""
    print("=" * 60)
    print("Standalone RTSP Live Stream Desktop App")
    print(f"Production Server: {PRODUCTION_SERVER_URL}")
    print(f"RTMP Server: {RTMP_SERVER_URL}")
    print(f"Cameras File: {CAMERAS_JSON_FILE}")
    print(f"Log File: {LOG_FILE}")
    print("=" * 60)

    logger.info("Application started")

    # Initialize stream manager
    global manager
    manager = StreamManager()

    # Register cleanup handlers
    atexit.register(cleanup_handler)

    # Register signal handlers for graceful shutdown
    try:
        signal.signal(signal.SIGTERM, signal_handler)  # Termination signal
        signal.signal(signal.SIGINT, signal_handler)   # Interrupt signal (Ctrl+C)
    except ValueError:
        # Signal handling might not work in all environments (like some Windows setups)
        logger.warning("Signal handling not available in this environment")

    # Check initial connections
    internet_ok = manager.check_internet_connection()
    server_ok = manager.check_server_connection() if internet_ok else False

    logger.info(f"Initial connection check: Internet={internet_ok}, Server={server_ok}")

    if not internet_ok:
        logger.warning("No internet connection detected. Will retry when connection is restored.")

    # Start monitoring
    manager.start_monitoring()

    # Start all streams
    manager.start_all_streams()

    try:
        # Keep running
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
        manager.stop_monitoring()
        manager.stop_all_streams("Application shutdown")
        logger.info("Application stopped")

if __name__ == "__main__":
    main()
