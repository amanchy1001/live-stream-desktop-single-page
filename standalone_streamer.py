import os

# ================== LOCK FILE (Prevent Multiple Instances) ==================
LOCK_FILE = 'streamer.lock'
import sys
import platform
def acquire_lock():
    if platform.system() == 'Windows':
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE, 'r') as f:
                    pid = int(f.read().strip())
                # Try to check if process exists
                try:
                    import psutil
                except ImportError:
                    print('psutil is required for Windows lock file support. Please install with "pip install psutil".')
                    sys.exit(1)
                if psutil.pid_exists(pid):
                    print('Another instance of the streamer is already running! Exiting.')
                    sys.exit(1)
            except Exception:
                pass
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
        return None
    else:
        import fcntl
        lock_fp = open(LOCK_FILE, 'w')
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_fp.write(str(os.getpid()))
            lock_fp.flush()
            return lock_fp
        except Exception:
            print('Another instance of the streamer is already running! Exiting.')
            sys.exit(1)

def release_lock(lock_fp):
    if platform.system() == 'Windows':
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass
    else:
        import fcntl
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
            lock_fp.close()
            os.remove(LOCK_FILE)
        except Exception:
            pass
"""
Standalone RTSP Live Stream Desktop App
Auto-connects to production server, handles internet disconnections, and manages streams automatically.
"""

import subprocess
import requests
import logging
import sys
import time
import threading
import socket
import atexit
import signal

# ================== CONFIGURATION ==================
# BACKEND_URL = "http://172.105.52.55:8011"  # Backend API URL
# RTMP_SERVER_URL = "rtmp://172.105.52.55:1935/live"  # RTMP server URL
BACKEND_URL = "http://172.105.52.55:8011"  # Backend API URL for local
RTMP_SERVER_URL = "rtmp://172.105.52.55/live"  # RTMP server URL for local
LOG_FILE = "stream_log.txt"

# Camera list: update here as needed
CAMERA_CONFIG = [
    # {"id": "camera_1", "name": "Main Door", "rtsp_url": "rtsp://admin:geron123@192.168.0.201:554/cam/realmonitor?channel=1"},
    # {"id": "camera_2", "name": "Main Area", "rtsp_url": "rtsp://admin:geron123@192.168.0.202:554/cam/realmonitor?channel=1"}
    {"id":"camera_1","name":"Inside Area 1","rtsp_url":"rtsp://admin:Geron%40123@192.168.31.3:554/Streaming/Channels/101"},
    {"id":"camera_2","name":"Main Gate Wide View","rtsp_url":"rtsp://admin:Geron%40123@192.168.31.4:554/Streaming/Channels/101"},
    {"id":"camera_3","name":"Inside Area 2","rtsp_url":"rtsp://admin:Geron%40123@192.168.31.2:554/Streaming/Channels/101"},
    {"id":"camera_4","name":"Mechine Area","rtsp_url":"rtsp://admin:Geron%40123@192.168.31.6:554/Streaming/Channels/101"}
    # Add more cameras as needed
]

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("auto_streamer")

# ================== STREAMER ==================
class Camera:
    def __init__(self, camera_id, name, rtsp_url):
        self.id = camera_id
        self.name = name
        self.rtsp_url = rtsp_url

class StreamProcess:
    def __init__(self, camera: Camera):
        self.camera = camera
        self.process = None
        self.last_start_time = 0

    def start(self):
        cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-i", self.camera.rtsp_url,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-b:v", "2500k",
            "-maxrate", "3000k",
            "-bufsize", "6000k",
            "-pix_fmt", "yuv420p",
            "-g", "50",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-f", "flv",
            "-flvflags", "no_duration_filesize",
            "-loglevel", "error",
            f"{RTMP_SERVER_URL.rstrip('/')}/{self.camera.id}"
        ]
        logger.info(f"Starting FFmpeg for {self.camera.name}: {' '.join(cmd)}")
        self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.last_start_time = time.time()

    def is_running(self):
        return self.process and self.process.poll() is None

    def stop(self):
        if self.process and self.is_running():
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

class AutoStreamer:
    def backend_hard_restart(self):
        """Call backend endpoint to hard restart/clean all streams."""
        try:
            logger.info("Calling backend hard restart endpoint...")
            self.session.post(f"{BACKEND_URL}/api/cleanup/restart", timeout=10)
        except Exception as e:
            logger.warning(f"Failed to call backend hard restart: {e}")

    def validate_and_cleanup_hls(self):
        """Periodically check and clean up old/corrupt HLS files."""
        try:
            for cam in self.cameras:
                stream_dir = os.path.join('..', 'backend', 'streams', cam.id)
                if os.path.exists(stream_dir):
                    for fname in os.listdir(stream_dir):
                        if fname.endswith('.ts') or fname.endswith('.m3u8'):
                            fpath = os.path.join(stream_dir, fname)
                            try:
                                if os.path.getsize(fpath) == 0:
                                    logger.warning(f"Deleting corrupt/empty HLS file: {fpath}")
                                    os.remove(fpath)
                            except Exception as e:
                                logger.warning(f"Error checking HLS file {fpath}: {e}")
        except Exception as e:
            logger.warning(f"HLS validation/cleanup error: {e}")
    def recover_backend_state(self):
        """On startup, call backend hard restart and stop all streams to clean up any stale state."""
        self.backend_hard_restart()
        logger.info("Recovering backend state: stopping all streams on backend...")
        for cam in self.cameras:
            try:
                payload = {"stream_id": cam.id}
                self.session.post(f"{BACKEND_URL}/api/streams/stop", json=payload, timeout=5)
            except Exception as e:
                logger.warning(f"Failed to notify backend to stop {cam.name} on startup: {e}")

    def retry_failed_notifications(self):
        """Retry any failed backend notifications from previous network loss."""
        if hasattr(self, 'failed_notifications'):
            for fn in self.failed_notifications[:]:
                try:
                    resp = self.session.post(fn['url'], json=fn['payload'], timeout=5)
                    if resp.status_code == 200:
                        self.failed_notifications.remove(fn)
                        logger.info(f"Retried and succeeded: {fn['url']} for {fn['payload']}")
                except Exception:
                    pass

    def log_failed_notification(self, url, payload):
        if not hasattr(self, 'failed_notifications'):
            self.failed_notifications = []
        self.failed_notifications.append({'url': url, 'payload': payload})
    def __init__(self):
        self.cameras = [Camera(c["id"], c["name"], c["rtsp_url"]) for c in CAMERA_CONFIG]
        self.streams = {cam.id: StreamProcess(cam) for cam in self.cameras}
        self.session = requests.Session()
        self.monitoring = True

    def check_internet(self):
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=5)
            return True
        except OSError:
            return False

    def check_backend(self):
        try:
            resp = self.session.get(f"{BACKEND_URL}/", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def notify_backend_start(self, camera: Camera):
        payload = {"stream_id": camera.id, "rtsp_url": camera.rtsp_url}
        try:
            resp = self.session.post(f"{BACKEND_URL}/api/streams/start", json=payload, timeout=5)
            if resp.status_code == 200:
                logger.info(f"Backend notified for {camera.name}")
                return True
            else:
                logger.warning(f"Backend start failed for {camera.name}: {resp.status_code}")
                self.log_failed_notification(f"{BACKEND_URL}/api/streams/start", payload)
                return False
        except Exception as e:
            logger.warning(f"Backend start error for {camera.name}: {e}")
            self.log_failed_notification(f"{BACKEND_URL}/api/streams/start", payload)
            return False

    def notify_backend_stop(self, camera: Camera):
        payload = {"stream_id": camera.id}
        try:
            self.session.post(f"{BACKEND_URL}/api/streams/stop", json=payload, timeout=5)
        except Exception as e:
            logger.warning(f"Backend stop error for {camera.name}: {e}")
            self.log_failed_notification(f"{BACKEND_URL}/api/streams/stop", payload)

    def start_all_streams(self):
        for cam_id, stream in self.streams.items():
            if not stream.is_running():
                logger.info(f"Starting stream for {stream.camera.name}")
                stream.start()
                self.notify_backend_start(stream.camera)

    def monitor(self):
        logger.info("Starting monitoring loop...")
        def thread_wrapper(target, *args, **kwargs):
            try:
                target(*args, **kwargs)
            except Exception as e:
                logger.error(f"Uncaught exception in thread: {e}")

        network_was_up = True
        camera_failures = {cam.id: 0 for cam in self.cameras}
        CAMERA_FAIL_THRESHOLD = 6  # e.g., 1 minute if check every 10s
        hls_cleanup_counter = 0
        while self.monitoring:
            internet = self.check_internet()
            backend = self.check_backend() if internet else False
            if internet and backend:
                self.retry_failed_notifications()
            # Network loss handling: stop all streams if network goes down
            if not internet or not backend:
                if network_was_up:
                    logger.warning("Network or backend lost! Stopping all streams and cleaning backend.")
                    for stream in self.streams.values():
                        stream.stop()
                    self.backend_hard_restart()
                    network_was_up = False
                time.sleep(5)
                continue
            else:
                if not network_was_up:
                    logger.info("Network restored. Cleaning backend and restarting all streams.")
                    self.backend_hard_restart()
                    for cam_id, stream in self.streams.items():
                        if not stream.is_running():
                            stream.start()
                            self.notify_backend_start(stream.camera)
                    network_was_up = True
            for cam_id, stream in self.streams.items():
                if not stream.is_running():
                    camera_failures[cam_id] += 1
                    logger.warning(f"Stream for {stream.camera.name} not running. Restarting...")
                    stream.stop()
                    time.sleep(2)
                    stream.start()
                    self.notify_backend_start(stream.camera)
                    # If camera fails too many times, notify backend to stop
                    if camera_failures[cam_id] >= CAMERA_FAIL_THRESHOLD:
                        logger.warning(f"Camera {stream.camera.name} unreachable for extended period. Notifying backend to stop stream.")
                        self.notify_backend_stop(stream.camera)
                        camera_failures[cam_id] = 0
                else:
                    camera_failures[cam_id] = 0
            hls_cleanup_counter += 1
            if hls_cleanup_counter % 6 == 0:  # Every ~1 minute
                t = threading.Thread(target=thread_wrapper, args=(self.validate_and_cleanup_hls,))
                t.start()
            time.sleep(10)

    def stop_all(self):
        logger.info("Stopping all streams...")
        for stream in self.streams.values():
            stream.stop()
            self.notify_backend_stop(stream.camera)
        self.monitoring = False

def cleanup_handler(streamer: AutoStreamer):
    logger.info("Exiting, cleaning up...")
    streamer.stop_all()

def main():
    lock_fp = acquire_lock()
    streamer = AutoStreamer()
    atexit.register(cleanup_handler, streamer)
    atexit.register(lambda: release_lock(lock_fp))
    try:
        signal.signal(signal.SIGTERM, lambda s, f: (cleanup_handler(streamer), release_lock(lock_fp), sys.exit(0)))
        signal.signal(signal.SIGINT, lambda s, f: (cleanup_handler(streamer), release_lock(lock_fp), sys.exit(0)))
    except Exception:
        pass
    streamer.recover_backend_state()
    streamer.start_all_streams()
    streamer.monitor()

if __name__ == "__main__":
    main()
