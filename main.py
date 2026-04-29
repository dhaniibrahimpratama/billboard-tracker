"""Billboard Eye Tracker — Production-grade Multiprocessing Pipeline with JSON IPC."""

import base64
import json
import multiprocessing as mp
from multiprocessing import shared_memory
import os
import time
import sys
import signal
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import cv2
import pandas as pd

# ==========================================
# 1. KONFIGURASI GLOBAL & PATH PYINSTALLER
# ==========================================

# Path Detection untuk PyInstaller (.exe)
if getattr(sys, 'frozen', False):
    # pylint: disable=protected-access
    BASE_DIR = Path(sys._MEIPASS)
    EXE_DIR = Path(os.path.dirname(sys.executable))
    OUTPUT_DIR = EXE_DIR / "output"
else:
    BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
    OUTPUT_DIR = BASE_DIR / "output"

OUTPUT_DIR.mkdir(exist_ok=True)

FRAME_W, FRAME_H, FRAME_C = 640, 480, 3
FRAME_SHAPE = (FRAME_H, FRAME_W, FRAME_C)
FRAME_DTYPE = np.uint8
FRAME_SIZE = int(np.prod(FRAME_SHAPE)) * np.dtype(FRAME_DTYPE).itemsize

# Ring Buffer Ganda
SHM_SLOTS = 2 
SHM_NAMES_IN = [f"shm_cam_in_{i}" for i in range(SHM_SLOTS)]   # Untreated raw frame
SHM_NAMES_OUT = [f"shm_cam_out_{i}" for i in range(SHM_SLOTS)] # Processed frame w/ bbox

COOLDOWN_MINUTES  = 5     
INTERVAL_MINUTES  = 10    

exit_event_global = None


# ==========================================
# 2. IPC JSON HELPERS
# ==========================================
def send(msg: dict):
    """Kirim JSON ke stdout untuk dibaca oleh Electron."""
    print(json.dumps(msg, ensure_ascii=False), flush=True)

def encode_frame(frame):
    """Encode numpy frame to base64 JPEG."""
    h, w = frame.shape[:2]
    if w > FRAME_W:
        ratio = FRAME_W / w
        frame = cv2.resize(frame, (FRAME_W, int(h * ratio)))
    _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return base64.b64encode(buf).decode('ascii')


# ==========================================
# 3. DEFINISI LOGICAL CLASS
# ==========================================
class CooldownTracker:
    def __init__(self, cooldown_minutes=COOLDOWN_MINUTES):
        self.cooldown    = timedelta(minutes=cooldown_minutes)
        self.last_seen   = {}   
        self.total_watch = 0    

    def check_and_register(self, track_id):
        now = datetime.now()
        if track_id not in self.last_seen:
            self.last_seen[track_id] = now
            self.total_watch += 1
            return True

        elapsed = now - self.last_seen[track_id]
        if elapsed >= self.cooldown:
            self.last_seen[track_id] = now
            self.total_watch += 1
            return True

        return False

    def reset_interval(self):
        self.total_watch = 0

class CSVLogger:
    def __init__(self):
        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path  = OUTPUT_DIR / f"billboard_{timestamp}.csv"
        self.rows  = []
        pd.DataFrame(columns=[
            "timestamp", "people_passing", "people_watching"
        ]).to_csv(self.path, index=False)
        send({"type": "info", "message": f"CSV output: {self.path}"})

    def log(self, people_passing, people_watching):
        row = {
            "timestamp"       : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "people_passing"  : people_passing,
            "people_watching" : people_watching,
        }
        self.rows.append(row)
        pd.DataFrame([row]).to_csv(self.path, mode="a", header=False, index=False)
        return row


# ==========================================
# 4. PRODUCER: CAMERA I/O PROCESS
# ==========================================
def camera_producer(exit_event, latest_in_idx, frame_ready_event, ai_ready_event, source=0):
    # Hijack signal, Worker mati patuh pada 'exit_event' bapaknya.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    backend = cv2.CAP_DSHOW if sys.platform == 'win32' else cv2.CAP_V4L2
    if isinstance(source, str):
        source = str(source).strip('"').strip("'").replace('\\', '/')
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    else:
        cap = cv2.VideoCapture(source, backend)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        devnull = open(os.devnull, 'w', encoding='utf-8')
        os.dup2(devnull.fileno(), sys.stderr.fileno())

    try:
        if not cap.isOpened():
            print(json.dumps({"type": "error", "message": f"Tidak bisa membuka sumber video: {source}"}), flush=True)
            return

        shm_blocks = [shared_memory.SharedMemory(name=name) for name in SHM_NAMES_IN]
        slot_idx = 0

        # Tunggu AI siap sebelum mulai memutar video (agar video pendek tidak terlewat)
        while not ai_ready_event.is_set() and not exit_event.is_set():
            time.sleep(0.1)

        frame_count = 0
        while not exit_event.is_set():
            if hasattr(mp, 'parent_process'):
                parent = mp.parent_process()
                if parent is not None and not parent.is_alive():
                    break
            
            ret, frame = cap.read()
            if not ret:
                if isinstance(source, str):
                    break 
                continue
            
            frame_count += 1
            
            if frame.shape != FRAME_SHAPE:
                frame = cv2.resize(frame, (FRAME_W, FRAME_H))

            shm_in = shm_blocks[slot_idx]
            dst = np.ndarray(FRAME_SHAPE, dtype=FRAME_DTYPE, buffer=shm_in.buf)
            np.copyto(dst, frame)
            
            with latest_in_idx.get_lock():
                latest_in_idx.value = slot_idx
            
            frame_ready_event.set()
            
            slot_idx = (slot_idx + 1) % SHM_SLOTS
            
            if isinstance(source, str):
                time.sleep(1/30)
    except Exception as e:
        err_msg = f"[Camera Producer Crash]: {repr(e)}"
        print(json.dumps({"type": "error", "message": err_msg}), flush=True)
        sys.stderr.write(err_msg + "\n")
        sys.stderr.flush()
    finally:
        if 'shm_blocks' in locals():
            for shm in shm_blocks: shm.close()
        if 'cap' in locals():
            cap.release()

# ==========================================
# 5. CONSUMER: AI WORKER PROCESS
# ==========================================
def ai_worker_process(exit_event, latest_in_idx, latest_out_idx, frame_ready_event, result_queue, ai_ready_event):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

    from scripts.people_counter import PeopleCounter
    from scripts.eye_tracker import EyeTracker
    
    counter  = PeopleCounter()
    tracker  = EyeTracker()
    cooldown = CooldownTracker()
    logger   = CSVLogger()

    # Beri tahu produser bahwa model sudah diload dan siap
    ai_ready_event.set()

    interval_start    = datetime.now()
    interval_passing  = 0
    interval_watching = 0

    known_shms_in = {name: shared_memory.SharedMemory(name=name) for name in SHM_NAMES_IN}
    known_shms_out = {name: shared_memory.SharedMemory(name=name) for name in SHM_NAMES_OUT}
    out_slot_idx = 0
    
    try:
        while not exit_event.is_set():
            if hasattr(mp, 'parent_process'):
                parent = mp.parent_process()
                if parent is not None and not parent.is_alive():
                    break

            if not frame_ready_event.wait(timeout=0.1):
                continue
            
            frame_ready_event.clear()
            
            with latest_in_idx.get_lock():
                in_target_slot = latest_in_idx.value
                
            shm_in = known_shms_in[SHM_NAMES_IN[in_target_slot]]
            frame_view = np.ndarray(FRAME_SHAPE, dtype=FRAME_DTYPE, buffer=shm_in.buf)
            frame_work = frame_view.copy()

            # --- 1. PEOPLE COUNTER ---
            frame_work, active_people, _ = counter.process_frame(frame_work)
            interval_passing = counter.count
            
            # --- 2. EYE TRACKER ---
            frame_work, faces = tracker.process_frame(frame_work)

            # --- 3. LOGGING COOLDOWN ---
            watching_now = 0
            for i, face in enumerate(faces):
                if face["looking"]:
                    watching_now += 1
                    face_id = f"face_{i}"
                    if cooldown.check_and_register(face_id):
                        interval_watching += 1

            # --- 4. FLUSH INTERVAL ---
            elapsed = datetime.now() - interval_start
            remaining = timedelta(minutes=INTERVAL_MINUTES) - elapsed
            rem_sec = max(0, int(remaining.total_seconds()))

            if elapsed >= timedelta(minutes=INTERVAL_MINUTES):
                row = logger.log(interval_passing, interval_watching)
                result_queue.put({"type": "csv_row", "row": row})
                
                counter.reset()
                cooldown.reset_interval()
                interval_passing = 0
                interval_watching = 0
                interval_start = datetime.now()

            # --- 5. TULIS HASIL KE SHM ---
            shm_out = known_shms_out[SHM_NAMES_OUT[out_slot_idx]]
            dst_out = np.ndarray(FRAME_SHAPE, dtype=FRAME_DTYPE, buffer=shm_out.buf)
            np.copyto(dst_out, frame_work)

            with latest_out_idx.get_lock():
                latest_out_idx.value = out_slot_idx

            # --- 6. KIRIM STATS KE MAIN ---
            result_queue.put({
                "type": "frame_ready",
                "active_people": active_people,
                "people_passing": interval_passing,
                "watching_now": watching_now,
                "people_watching": interval_watching,
                "flush_in_seconds": rem_sec
            })
            out_slot_idx = (out_slot_idx + 1) % SHM_SLOTS
            
    finally:
        logger.log(interval_passing, interval_watching)
        for shm in known_shms_in.values(): shm.close()
        for shm in known_shms_out.values(): shm.close()


# ==========================================
# 6. MAIN THREAD & GRACEFUL SHUTDOWN
# ==========================================
# pylint: disable=unused-argument
def shutdown_handler(signum, frame):
    """Callback for OS termination signals."""
    if exit_event_global is not None:
        exit_event_global.set()

def main():
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        try:
            source = int(arg)
        except ValueError:
            source = arg
    else:
        source = 0

    mp.set_start_method('spawn', force=True)
    
    global exit_event_global
    exit_event_global = mp.Event() 
    latest_in_idx     = mp.Value('i', 0)     
    latest_out_idx    = mp.Value('i', 0)
    frame_ready_event = mp.Event()         
    result_queue      = mp.Queue()
    
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    shm_blocks_all = []
    
    def allocate_shm(names):
        blocks = []
        for name in names:
            try:
                shm = shared_memory.SharedMemory(create=True, name=name, size=FRAME_SIZE)
            except getattr(shared_memory, 'SharedMemoryError', FileExistsError):
                shm = shared_memory.SharedMemory(name=name)
            blocks.append(shm)
        return blocks

    shm_blocks_in  = allocate_shm(SHM_NAMES_IN)
    shm_blocks_out = allocate_shm(SHM_NAMES_OUT)
    shm_blocks_all.extend(shm_blocks_in + shm_blocks_out)

    ai_ready_event = mp.Event()

    producer_process = mp.Process(target=camera_producer, args=(exit_event_global, latest_in_idx, frame_ready_event, ai_ready_event, source))
    ai_process       = mp.Process(target=ai_worker_process, args=(exit_event_global, latest_in_idx, latest_out_idx, frame_ready_event, result_queue, ai_ready_event))
    
    producer_process.start()
    ai_process.start()

    send({"type": "ready"})
    
    # Target fps limiter for sending frames to frontend to avoid overloading IPC
    frame_interval = 1.0 / 30.0 
    
    try:
        while not exit_event_global.is_set():
            t_start = time.time()
            if not result_queue.empty():
                result = result_queue.get()
                
                if result.get("type") == "csv_row":
                    send({
                        "type": "csv_row",
                        "timestamp": result["row"]["timestamp"],
                        "people_passing": result["row"]["people_passing"],
                        "people_watching": result["row"]["people_watching"]
                    })
                    continue
                
                # Zero-Copy fetching dari AI
                with latest_out_idx.get_lock():
                    out_target_slot = latest_out_idx.value
                
                shm_out = shm_blocks_out[out_target_slot]
                frame_view = np.ndarray(FRAME_SHAPE, dtype=FRAME_DTYPE, buffer=shm_out.buf)
                
                # Kirim ke Electron
                b64 = encode_frame(frame_view.copy())
                send({"type": "frame", "data": b64})
                send({
                    "type": "stats",
                    "active_people": result["active_people"],
                    "people_passing": result["people_passing"],
                    "watching_now": result["watching_now"],
                    "people_watching": result["people_watching"],
                    "flush_in_seconds": result["flush_in_seconds"]
                })

            # Check if producer died (e.g. video ended)
            if not producer_process.is_alive():
                send({"type": "done", "message": "Video selesai."})
                exit_event_global.set()
                break

            # Frame pacing
            dt = time.time() - t_start
            if dt < frame_interval:
                time.sleep(frame_interval - dt)
                
    except Exception as e: # pylint: disable=broad-exception-caught
        send({"type": "error", "message": str(e)})
        exit_event_global.set()
    
    finally:
        producer_process.join(timeout=2)
        ai_process.join(timeout=2)
        
        if producer_process.is_alive(): producer_process.terminate()
        if ai_process.is_alive(): ai_process.terminate()
        
        for shm in shm_blocks_all:
            shm.close()
            try:
                shm.unlink()
            except Exception: pass # pylint: disable=broad-exception-caught
            
        sys.exit(0)

if __name__ == "__main__":
    main()
