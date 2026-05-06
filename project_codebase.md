# Project Codebase

## `billboard_backend.spec`

```
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = [('yolov8n.pt', '.'), ('scripts', 'scripts')]
datas += collect_data_files('mediapipe')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='billboard_backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='billboard_backend',
)
```

## `main.py`

```python
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
            if hasattr(mp, 'parent_process'):
                parent = mp.parent_process()
                if parent is not None and not parent.is_alive():
                    return
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
    interval_passing  = 0
    interval_watching = 0
    try:
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

        known_shms_in = {name: shared_memory.SharedMemory(name=name) for name in SHM_NAMES_IN}
        known_shms_out = {name: shared_memory.SharedMemory(name=name) for name in SHM_NAMES_OUT}
        out_slot_idx = 0
    
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
            
    except Exception as e:
        err_msg = f"[AI Worker Crash]: {repr(e)}"
        print(json.dumps({"type": "error", "message": err_msg}), flush=True)
        sys.stderr.write(err_msg + "\n")
        sys.stderr.flush()
    finally:
        if 'logger' in locals():
            logger.log(interval_passing, interval_watching)
        if 'known_shms_in' in locals():
            for shm in known_shms_in.values(): shm.close()
        if 'known_shms_out' in locals():
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
    mp.freeze_support()
    main()
```

## `requirements.txt`

```
opencv-python
mediapipe
ultralytics
pandas
numpy
```

## `billboard-fe\electron-builder.yml`

```
appId: com.electron.billboardtracker
productName: Billboard Eye Tracker
directories:
  output: dist
  buildResources: build
files:
  - out/**/*
  - package.json
extraResources:
  - from: resources/billboard_backend
    to: billboard_backend
```

## `billboard-fe\package.json`

```json
{
  "name": "billboard-fe",
  "version": "1.0.0",
  "main": "./out/main/index.js",
  "scripts": {
    "dev": "electron-vite dev",
    "build": "electron-vite build",
    "build:win": "npm run build && electron-builder --win"
  },
  "dependencies": {
    "@electron-toolkit/preload": "^3.0.2",
    "@electron-toolkit/utils": "^4.0.0"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.2.1",
    "electron": "^30.0.0",
    "electron-builder": "^24.13.3",
    "electron-vite": "^2.3.0",
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "vite": "^5.2.0"
  }
}
```

## `billboard-fe\src\main\index.js`

```javascript
import { app, shell, BrowserWindow, ipcMain } from 'electron'
import { join } from 'path'
import { electronApp, optimizer, is } from '@electron-toolkit/utils'
import { spawn } from 'child_process'

let pyProcess = null;

function createWindow() {
  const mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false
    }
  })

  mainWindow.on('ready-to-show', () => {
    mainWindow.show()
  })

  mainWindow.webContents.setWindowOpenHandler((details) => {
    shell.openExternal(details.url)
    return { action: 'deny' }
  })

  if (is.dev && process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

app.whenReady().then(() => {
  electronApp.setAppUserModelId('com.electron')

  app.on('browser-window-created', (_, window) => {
    optimizer.watchWindowShortcuts(window)
  })

  ipcMain.handle('start-python', (event, source) => {
    if (pyProcess) {
      try { pyProcess.kill() } catch (e) {}
      pyProcess = null
    }

    let scriptPath;
    let command;
    let args = [];
    
    if (app.isPackaged) {
      scriptPath = join(process.resourcesPath, 'billboard_backend', 'billboard_backend.exe')
      command = scriptPath
      args = [source.toString()]
    } else {
      scriptPath = join(app.getAppPath(), '..', 'main.py')
      command = 'python'
      args = ['-u', scriptPath, source.toString()]
    }

    console.log(`Starting python backend: ${command} ${args.join(' ')}`);
    
    const cwd = app.isPackaged ? process.resourcesPath : join(app.getAppPath(), '..');

    pyProcess = spawn(command, args, { cwd: cwd })

    let dataBuffer = '';
    pyProcess.stdout.on('data', (data) => {
      dataBuffer += data.toString();
      const lines = dataBuffer.split('\n');
      dataBuffer = lines.pop(); // keep incomplete line
      lines.forEach(line => {
        if (!line.trim()) return;
        try {
          const msg = JSON.parse(line)
          BrowserWindow.getAllWindows()[0]?.webContents.send('python-message', msg)
        } catch (e) {
          console.log(`[Python Log]: ${line}`)
        }
      })
    })

    pyProcess.stderr.on('data', (data) => {
      console.error(`[Python Error]: ${data.toString()}`)
    })

    const currentProcess = pyProcess;
    pyProcess.on('close', (code) => {
      if (pyProcess === currentProcess) {
        console.log(`Python process exited with code ${code}`)
        BrowserWindow.getAllWindows()[0]?.webContents.send('python-message', { type: 'done', message: `Process exited (${code})` })
      }
    })

    return true
  })

  ipcMain.handle('stop-python', () => {
    if (pyProcess) {
      try { pyProcess.kill() } catch (e) {}
      pyProcess = null
    }
    return true
  })

  createWindow()

  app.on('activate', function () {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (pyProcess) {
    pyProcess.kill()
  }
  if (process.platform !== 'darwin') {
    app.quit()
  }
})
```

## `billboard-fe\src\preload\index.js`

```javascript
import { contextBridge, ipcRenderer } from 'electron'
import { electronAPI } from '@electron-toolkit/preload'

const api = {
  startPython: (source) => ipcRenderer.invoke('start-python', source),
  stopPython: () => ipcRenderer.invoke('stop-python'),
  onPythonMessage: (callback) => ipcRenderer.on('python-message', (_event, value) => callback(value)),
  removePythonListener: () => ipcRenderer.removeAllListeners('python-message')
}

if (process.contextIsolated) {
  try {
    contextBridge.exposeInMainWorld('electron', electronAPI)
    contextBridge.exposeInMainWorld('api', api)
  } catch (error) {
    console.error(error)
  }
} else {
  window.electron = electronAPI
  window.api = api
}
```

## `billboard-fe\src\renderer\index.html`

```html
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Billboard Eye Tracker</title>
    <!-- Content Security Policy -->
    <meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:" />
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
```

## `billboard-fe\src\renderer\src\App.jsx`

```javascript
import React, { useState, useEffect } from 'react'

function App() {
  const [status, setStatus] = useState('IDLE') // IDLE, RUNNING, ERROR
  const [sourceName, setSourceName] = useState('')
  const [frameData, setFrameData] = useState(null)
  
  const [stats, setStats] = useState({
    active_people: 0,
    people_passing: 0,
    watching_now: 0,
    people_watching: 0,
    flush_in_seconds: 600
  })

  const [csvLogs, setCsvLogs] = useState([])

  useEffect(() => {
    // Listen to IPC messages from main process
    const removeListener = window.api.onPythonMessage((msg) => {
      if (!msg) return;

      if (msg.type === 'ready') {
        setStatus('RUNNING')
      } else if (msg.type === 'frame') {
        setFrameData(`data:image/jpeg;base64,${msg.data}`)
      } else if (msg.type === 'stats') {
        setStats(msg)
      } else if (msg.type === 'csv_row') {
        setCsvLogs(prev => [
          {
            time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
            passing: msg.people_passing,
            watching: msg.people_watching
          },
          ...prev
        ].slice(0, 50)) // Keep last 50 logs
      } else if (msg.type === 'done') {
        setStatus('IDLE')
        setFrameData(null)
      } else if (msg.type === 'error') {
        setStatus('ERROR')
        setFrameData(null)
      }
    })

    return () => {
      if (window.api && window.api.removePythonListener) {
        window.api.removePythonListener()
      }
    }
  }, [])

  const startWebcam = async () => {
    setStatus('RUNNING')
    setSourceName('Kamera (Webcam)')
    setFrameData(null)
    setCsvLogs([])
    await window.api.startPython(0)
  }

  const handleFileUpload = async (event) => {
    const file = event.target.files[0]
    if (file) {
      setStatus('RUNNING')
      setSourceName(file.name)
      setFrameData(null)
      setCsvLogs([])
      await window.api.startPython(file.path)
    }
  }

  const stopTracker = async () => {
    await window.api.stopPython()
    setStatus('IDLE')
    setFrameData(null)
  }

  const formatTime = (seconds) => {
    const m = Math.floor(seconds / 60).toString().padStart(2, '0')
    const s = (seconds % 60).toString().padStart(2, '0')
    return `${m}:${s}`
  }

  // Calculate percentage (600 seconds = 10 minutes max)
  const progressPercent = Math.min(100, Math.max(0, (stats.flush_in_seconds / 600) * 100))

  return (
    <div className="app-container">
      <header>
        <div className="brand">
          <span>Billboard</span> Eye Tracker
        </div>
        <div className="header-actions">
          <button 
            className="btn btn-webcam" 
            onClick={startWebcam}
            disabled={status === 'RUNNING'}
            style={{ opacity: status === 'RUNNING' ? 0.5 : 1 }}
          >
            ▶ Live Webcam
          </button>
          
          <label 
            className="btn btn-upload"
            style={{ opacity: status === 'RUNNING' ? 0.5 : 1, cursor: status === 'RUNNING' ? 'default' : 'pointer' }}
          >
            ↑ Upload Video
            <input 
              type="file" 
              accept="video/mp4,video/avi,video/mkv" 
              onChange={handleFileUpload}
              disabled={status === 'RUNNING'}
            />
          </label>

          {status === 'RUNNING' && (
            <button className="btn btn-stop" onClick={stopTracker}>
              ■ Stop
            </button>
          )}
        </div>
      </header>

      <div className="main-content">
        <section className="video-section">
          <div className="video-feed">
            {frameData ? (
              <img src={frameData} alt="AI Camera Feed" />
            ) : (
              <div className="video-placeholder">
                <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="2" y="7" width="20" height="15" rx="2" ry="2"></rect>
                  <polyline points="17 2 12 7 7 2"></polyline>
                </svg>
                <span>Pilih mode Live Webcam atau Upload Video</span>
              </div>
            )}
          </div>
        </section>

        <section className="sidebar">
          
          <div className="status-wrapper">
            <div className="section-title">STATUS</div>
            <div className="status-badge">
              <div className={`status-indicator ${status.toLowerCase()}`}></div>
              {status === 'RUNNING' ? 'Berjalan' : status === 'ERROR' ? 'Error' : 'Dihentikan'}
            </div>
            {sourceName && (
              <div className="source-text">Sumber: {sourceName}</div>
            )}
          </div>

          <div className="stats-wrapper">
            <div className="section-title">STATISTIK INTERVAL INI</div>
            <div className="stats-grid">
              <div className="stat-box">
                <span className="stat-value cyan">{stats.active_people}</span>
                <span className="stat-label">Di frame</span>
              </div>
              <div className="stat-box">
                <span className="stat-value yellow">{stats.people_passing}</span>
                <span className="stat-label">Total lewat</span>
              </div>
              <div className="stat-box">
                <span className="stat-value green">{stats.watching_now}</span>
                <span className="stat-label">Lihat sekarang</span>
              </div>
              <div className="stat-box">
                <span className="stat-value green">{stats.people_watching}</span>
                <span className="stat-label">Total lihat</span>
              </div>
            </div>
          </div>

          <div className="progress-container">
            <div className="progress-header">
              <div className="section-title" style={{margin: 0}}>FLUSH CSV DALAM</div>
              <div className="progress-time">{formatTime(stats.flush_in_seconds)}</div>
            </div>
            <div className="progress-track">
              <div 
                className="progress-fill" 
                style={{ width: `${progressPercent}%` }}
              ></div>
            </div>
          </div>

          <div className="log-section">
            <div className="section-title">LOG CSV</div>
            <div className="table-container">
              <table>
                <thead>
                  <tr>
                    <th>Waktu</th>
                    <th>Lewat</th>
                    <th>Lihat</th>
                  </tr>
                </thead>
                <tbody>
                  {csvLogs.length === 0 ? (
                    <tr>
                      <td colSpan="3">
                        <div className="empty-table">Belum ada data</div>
                      </td>
                    </tr>
                  ) : (
                    csvLogs.map((log, i) => (
                      <tr key={i}>
                        <td>{log.time}</td>
                        <td>{log.passing}</td>
                        <td>{log.watching}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>

        </section>
      </div>
    </div>
  )
}

export default App
```

## `billboard-fe\src\renderer\src\main.jsx`

```javascript
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './assets/index.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
```

## `billboard-fe\src\renderer\src\assets\index.css`

```css
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
  --bg-color: #0f111a;
  --panel-bg: #1a1d2d;
  --text-main: #f8fafc;
  --text-muted: #8b95a5;
  --text-blue: #38bdf8;
  --accent-cyan: #06b6d4;
  --accent-yellow: #eab308;
  --accent-green: #22c55e;
  --accent-red: #ef4444;
  --border-color: rgba(255, 255, 255, 0.05);
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  padding: 0;
  font-family: 'Inter', sans-serif;
  background-color: var(--bg-color);
  color: var(--text-main);
  height: 100vh;
  overflow: hidden;
}

.app-container {
  display: flex;
  flex-direction: column;
  height: 100vh;
}

/* HEADER */
header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 1rem 1.5rem;
  background-color: #141722;
  border-bottom: 1px solid var(--border-color);
}

.brand {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--text-main);
}
.brand span {
  color: var(--accent-cyan);
}

.header-actions {
  display: flex;
  gap: 0.75rem;
}

.btn {
  padding: 0.5rem 1rem;
  border-radius: 6px;
  border: none;
  font-family: inherit;
  font-weight: 600;
  font-size: 0.9rem;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  transition: opacity 0.2s;
}

.btn:hover {
  opacity: 0.9;
}

.btn-webcam {
  background-color: var(--accent-cyan);
  color: #000;
}

.btn-upload {
  background-color: var(--accent-yellow);
  color: #000;
}

.btn-stop {
  background-color: var(--accent-red);
  color: white;
}

input[type="file"] {
  display: none;
}

/* MAIN LAYOUT */
.main-content {
  display: flex;
  flex: 1;
  overflow: hidden;
}

/* VIDEO AREA */
.video-section {
  flex: 1;
  padding: 1.5rem;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: #000;
}

.video-feed {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.video-feed img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

.video-placeholder {
  color: var(--text-blue);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1rem;
}

/* SIDEBAR */
.sidebar {
  width: 380px;
  background-color: var(--bg-color);
  border-left: 1px solid var(--border-color);
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
  overflow-y: auto;
}

.section-title {
  font-size: 0.75rem;
  font-weight: 700;
  color: var(--text-blue);
  text-transform: uppercase;
  letter-spacing: 1px;
  margin-bottom: 0.75rem;
}

/* STATUS SECTION */
.status-wrapper {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.status-badge {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-weight: 500;
}

.status-indicator {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background-color: var(--text-muted);
}
.status-indicator.running {
  background-color: var(--accent-green);
  box-shadow: 0 0 8px var(--accent-green);
}
.status-indicator.error {
  background-color: var(--accent-red);
  box-shadow: 0 0 8px var(--accent-red);
}

.source-text {
  font-size: 0.8rem;
  color: var(--text-blue);
  opacity: 0.8;
  word-break: break-all;
}

/* STATS GRID */
.stats-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
}

.stat-box {
  background-color: var(--panel-bg);
  border-radius: 8px;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
}

.stat-value {
  font-size: 2rem;
  font-weight: 700;
}

.stat-value.cyan { color: var(--accent-cyan); }
.stat-value.yellow { color: var(--accent-yellow); }
.stat-value.green { color: var(--accent-green); }

.stat-label {
  font-size: 0.75rem;
  color: var(--text-muted);
}

/* PROGRESS BAR */
.progress-container {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.progress-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.progress-time {
  font-weight: 700;
  color: var(--accent-yellow);
}

.progress-track {
  width: 100%;
  height: 6px;
  background-color: var(--panel-bg);
  border-radius: 3px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background-color: var(--accent-yellow);
  transition: width 1s linear;
}

/* LOG TABLE */
.log-section {
  display: flex;
  flex-direction: column;
  flex: 1;
}

.table-container {
  background-color: var(--panel-bg);
  border-radius: 8px;
  overflow: hidden;
  flex: 1;
  min-height: 200px;
}

table {
  width: 100%;
  border-collapse: collapse;
}

th {
  background-color: rgba(255, 255, 255, 0.05);
  color: var(--text-muted);
  font-size: 0.75rem;
  font-weight: 600;
  text-align: left;
  padding: 0.75rem 1rem;
}

td {
  padding: 0.75rem 1rem;
  font-size: 0.85rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.02);
}

.empty-table {
  padding: 2rem;
  text-align: center;
  color: var(--text-muted);
  font-size: 0.85rem;
}
```

## `scripts\eye_tracker.py`

```python
"""Module for eye tracking using MediaPipe."""

from datetime import datetime
import cv2
import mediapipe as mp
import numpy as np

# ─────────────────────────────────────────────
# KONFIGURASI
# ─────────────────────────────────────────────

# Index landmark MediaPipe FaceMesh (dari 468 titik)
LEFT_IRIS   = [474, 475, 476, 477]
RIGHT_IRIS  = [469, 470, 471, 472]

# format: [kiri, kanan, atas-luar, bawah-luar, atas-dalam, bawah-dalam]
LEFT_EYE    = [362, 263, 387, 380, 373, 385]
RIGHT_EYE   = [33,  133, 160, 144, 158, 153]

LOOKING_THRESHOLD = 0.25   # makin kecil = makin ketat
EAR_THRESHOLD     = 0.20   # eye aspect ratio minimum

class EyeTracker:
    """Tracker for detecting gaze direction using MediaPipe FaceMesh."""
    def __init__(self):
        print("[INFO] Loading MediaPipe FaceMesh...")
        # pylint: disable=no-member
        self.mp_face   = mp.solutions.face_mesh
        self.mp_draw   = mp.solutions.drawing_utils

        # pylint: disable=no-member
        self.face_mesh = self.mp_face.FaceMesh(
            max_num_faces        = 10,
            refine_landmarks     = True,
            min_detection_confidence = 0.5,
            min_tracking_confidence  = 0.5,
        )

    def _landmark_point(self, landmarks, idx, w, h):
        lm = landmarks[idx]
        return int(lm.x * w), int(lm.y * h)

    def _eye_aspect_ratio(self, landmarks, eye_indices, w, h):
        pts = [self._landmark_point(landmarks, i, w, h) for i in eye_indices]
        v1 = np.linalg.norm(np.array(pts[2]) - np.array(pts[3]))
        v2 = np.linalg.norm(np.array(pts[4]) - np.array(pts[5]))
        hz = np.linalg.norm(np.array(pts[0]) - np.array(pts[1]))
        if hz == 0:
            return 0
        return (v1 + v2) / (2.0 * hz)

    def _iris_offset(self, landmarks, iris_indices, eye_indices, w, h):
        iris_pts = [self._landmark_point(landmarks, i, w, h)
                    for i in iris_indices]
        eye_pts  = [self._landmark_point(landmarks, i, w, h)
                    for i in eye_indices]

        iris_center = np.mean(iris_pts, axis=0)
        eye_left    = np.array(eye_pts[0])
        eye_right   = np.array(eye_pts[1])
        eye_center  = (eye_left + eye_right) / 2.0
        eye_width   = np.linalg.norm(eye_right - eye_left)

        if eye_width == 0:
            return 0, 0

        offset_x = (iris_center[0] - eye_center[0]) / eye_width
        offset_y = (iris_center[1] - eye_center[1]) / eye_width
        return float(offset_x), float(offset_y)

    def process_frame(self, frame):
        """Terima 1 frame, deteksi semua wajah dan arah pandangan."""
        h, w = frame.shape[:2]
        rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.face_mesh.process(rgb)

        faces_data = []

        if not results.multi_face_landmarks:
            return frame, faces_data

        for face_landmarks in results.multi_face_landmarks:
            lms = face_landmarks.landmark

            ear_l = self._eye_aspect_ratio(lms, LEFT_EYE,  w, h)
            ear_r = self._eye_aspect_ratio(lms, RIGHT_EYE, w, h)
            eyes_open = (ear_l > EAR_THRESHOLD) and (ear_r > EAR_THRESHOLD)

            lox, loy = self._iris_offset(lms, LEFT_IRIS,  LEFT_EYE,  w, h)
            rox, roy = self._iris_offset(lms, RIGHT_IRIS, RIGHT_EYE, w, h)

            avg_ox = (abs(lox) + abs(rox)) / 2.0
            avg_oy = (abs(loy) + abs(roy)) / 2.0

            looking = (
                eyes_open
                and avg_ox < LOOKING_THRESHOLD
                and avg_oy < LOOKING_THRESHOLD
            )

            xs = [int(lm.x * w) for lm in lms]
            ys = [int(lm.y * h) for lm in lms]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)

            faces_data.append({
                "looking"      : looking,
                "face_box"     : (x1, y1, x2, y2),
                "left_offset"  : (lox, loy),
                "right_offset" : (rox, roy),
                "ear_left"     : ear_l,
                "ear_right"    : ear_r,
            })

            color  = (0, 255, 0) if looking else (0, 0, 255)
            label  = "LIHAT" if looking else "tidak lihat"

            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, label, (x1, y1 - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)

            for idx in LEFT_IRIS:
                px, py = self._landmark_point(lms, idx, w, h)
                cv2.circle(frame, (px, py), 2, (255, 200, 0), -1)

            for idx in RIGHT_IRIS:
                px, py = self._landmark_point(lms, idx, w, h)
                cv2.circle(frame, (px, py), 2, (255, 200, 0), -1)

            cv2.putText(
                frame,
                f"ox:{avg_ox:.2f} oy:{avg_oy:.2f}  EAR:{(ear_l+ear_r)/2:.2f}",
                (x1, y2 + 18),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1,
            )

        return frame, faces_data

if __name__ == "__main__":
    import sys
    tracker = EyeTracker()
    SOURCE = 0

    cap = cv2.VideoCapture(SOURCE)
    if not cap.isOpened():
        print("[ERROR] Tidak bisa membuka sumber video.")
        sys.exit(1)

    print("[INFO] Tekan 'Q' untuk keluar.")
    print("[INFO] Kotak HIJAU = lagi lihat kamera | Kotak MERAH = tidak lihat")

    while True:
        ret, current_frame = cap.read()
        if not ret:
            break

        current_frame, current_faces = tracker.process_frame(current_frame)

        for i, face in enumerate(current_faces):
            if face["looking"]:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      f"Wajah #{i+1} terdeteksi LIHAT ke kamera")

        cv2.imshow("Eye Tracker — tekan Q untuk keluar", current_frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()
```

## `scripts\people_counter.py`

```python
"""Module for people counting using YOLOv8."""

import os
import sys
from datetime import datetime
import cv2
from ultralytics import YOLO

# ─────────────────────────────────────────────
# KONFIGURASI & PATH DETECTION
# ─────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    # Packaged mode with PyInstaller
    # pylint: disable=protected-access
    MODEL_PATH = os.path.join(sys._MEIPASS, "yolov8n.pt")
else:
    # Dev mode
    MODEL_PATH = "yolov8n.pt"

CONFIDENCE   = 0.4            # minimum confidence deteksi
TARGET_CLASS = 0              # class 0 = "person" di COCO dataset


class PeopleCounter:
    """Tracker for counting unique people using YOLO and ByteTrack."""
    def __init__(self):
        print("[INFO] Loading model YOLOv8...")
        self.model       = YOLO(MODEL_PATH)
        self.tracked_ids = set()   # semua ID yang pernah masuk frame
        self.count       = 0       # total orang yang pernah terdeteksi

    def process_frame(self, frame):
        """
        Terima 1 frame, jalankan tracking, return:
          - frame yang sudah digambar box-nya
          - jumlah orang di frame ini (aktif)
          - total orang unik sejak mulai
        """
        results = self.model.track(
            source      = frame,
            persist     = True,        # wajib True agar ID konsisten antar frame
            tracker     = "bytetrack.yaml",
            classes     = [TARGET_CLASS],
            conf        = CONFIDENCE,
            verbose     = False,       # matikan log per-frame biar ga berisik
        )

        active_count = 0

        if results and results[0].boxes is not None:
            boxes = results[0].boxes

            for box in boxes:
                # skip kalau belum ada track ID (frame pertama kadang belum assign)
                if box.id is None:
                    continue

                track_id   = int(box.id.item())
                confidence = float(box.conf.item())
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())

                # kalau ID ini belum pernah kita lihat → tambah counter
                if track_id not in self.tracked_ids:
                    self.tracked_ids.add(track_id)
                    self.count += 1
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                          f"Orang baru terdeteksi! ID={track_id} | "
                          f"Total unik: {self.count}")

                active_count += 1

                # ── gambar bounding box ──────────────────────────────────
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(
                    frame,
                    f"ID:{track_id} ({confidence:.2f})",
                    (x1, y1 - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2
                )

        return frame, active_count, self.count

    def reset(self):
        """Reset counter (dipanggil tiap 10 menit oleh aggregator nanti)."""
        self.tracked_ids.clear()
        self.count = 0


if __name__ == "__main__":
    counter = PeopleCounter()

    # ganti 0 → path video kalau mau test pakai file, contoh: "test.mp4"
    SOURCE = 0

    cap = cv2.VideoCapture(SOURCE)
    if not cap.isOpened():
        print("[ERROR] Tidak bisa membuka sumber video. "
              "Pastikan webcam terhubung atau path video benar.")
        sys.exit(1)

    print("[INFO] Tekan 'Q' untuk keluar.")

    while True:
        ret, current_frame = cap.read()
        if not ret:
            print("[INFO] Video selesai / frame tidak terbaca.")
            break

        current_frame, active, total = counter.process_frame(current_frame)

        cv2.imshow("People Counter — tekan Q untuk keluar", current_frame)

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()
    print(f"\n[SELESAI] Total orang unik terdeteksi: {counter.count}")
```

## `scripts\__init__.py`

```python
"""Scripts package for Billboard Eye Tracker"""
```

