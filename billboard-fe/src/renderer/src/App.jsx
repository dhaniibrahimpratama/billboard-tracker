import React, { useState, useEffect, useRef } from 'react'

function App() {
  const [status, setStatus] = useState('IDLE') // IDLE, RUNNING, ERROR
  const [message, setMessage] = useState('Sistem siap.')
  const [frameData, setFrameData] = useState(null)
  
  const [stats, setStats] = useState({
    active_people: 0,
    people_passing: 0,
    watching_now: 0,
    people_watching: 0,
    flush_in_seconds: 600
  })

  const [logs, setLogs] = useState([])
  const logsEndRef = useRef(null)

  useEffect(() => {
    // Listen to IPC messages from main process
    const removeListener = window.api.onPythonMessage((msg) => {
      if (!msg) return;

      if (msg.type === 'ready') {
        setStatus('RUNNING')
        setMessage('Model aktif. Menganalisis...')
        addLog('INFO', 'AI Backend berjalan.')
      } else if (msg.type === 'frame') {
        setFrameData(`data:image/jpeg;base64,${msg.data}`)
      } else if (msg.type === 'stats') {
        setStats(msg)
      } else if (msg.type === 'csv_row') {
        addLog('CSV', `Data terekam: ${msg.people_passing} lewat, ${msg.people_watching} lihat`)
      } else if (msg.type === 'done') {
        setStatus('IDLE')
        setMessage(msg.message || 'Selesai.')
        addLog('INFO', 'Video stream selesai.')
      } else if (msg.type === 'error') {
        setStatus('ERROR')
        setMessage('Terjadi kesalahan!')
        addLog('ERROR', msg.message)
      } else if (msg.type === 'info') {
        addLog('INFO', msg.message)
      }
    })

    return () => {
      if (window.api && window.api.removePythonListener) {
        window.api.removePythonListener()
      }
    }
  }, [])

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logs])

  const addLog = (type, text) => {
    const time = new Date().toLocaleTimeString()
    setLogs(prev => [...prev.slice(-49), `[${time}] [${type}] ${text}`])
  }

  const startWebcam = async () => {
    setStatus('RUNNING')
    setMessage('Memulai kamera...')
    setFrameData(null)
    setLogs([])
    await window.api.startPython(0)
  }

  const handleFileUpload = async (event) => {
    const file = event.target.files[0]
    if (file) {
      setStatus('RUNNING')
      setMessage(`Memutar video: ${file.name}`)
      setFrameData(null)
      setLogs([])
      await window.api.startPython(file.path)
    }
  }

  const stopTracker = async () => {
    await window.api.stopPython()
    setStatus('IDLE')
    setMessage('Dihentikan.')
    setFrameData(null)
  }

  const formatTime = (seconds) => {
    const m = Math.floor(seconds / 60).toString().padStart(2, '0')
    const s = (seconds % 60).toString().padStart(2, '0')
    return `${m}:${s}`
  }

  return (
    <div className="app-container">
      <header>
        <h1>Billboard AI Tracker</h1>
        <div className={`status-badge ${status.toLowerCase()}`}>
          <div className="status-indicator"></div>
          {status === 'RUNNING' ? 'Aktif' : status === 'ERROR' ? 'Error' : 'Standby'}
        </div>
      </header>

      <div className="main-content">
        <section className="video-section">
          <div className="glass-card">
            <div className="controls">
              <button 
                className="btn btn-primary" 
                onClick={startWebcam}
                disabled={status === 'RUNNING'}
              >
                ▶ Live Webcam
              </button>
              
              <label className={`btn btn-secondary ${status === 'RUNNING' ? 'disabled' : ''}`}>
                📁 Upload Video
                <input 
                  type="file" 
                  accept="video/mp4,video/avi,video/mkv" 
                  onChange={handleFileUpload}
                  disabled={status === 'RUNNING'}
                />
              </label>

              {status === 'RUNNING' && (
                <button className="btn btn-danger" onClick={stopTracker}>
                  ■ Hentikan
                </button>
              )}
            </div>
          </div>

          <div className="video-feed">
            {frameData ? (
              <img src={frameData} alt="AI Camera Feed" />
            ) : (
              <div className="video-placeholder">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M23 7l-7 5 7 5V7z"></path>
                  <rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>
                </svg>
                <span>{message}</span>
              </div>
            )}
          </div>
        </section>

        <section className="dashboard-section">
          <div className="glass-card">
            <h2 style={{marginTop: 0, fontSize: '1.2rem', fontWeight: 600}}>Statistik Realtime</h2>
            <p style={{fontSize: '0.85rem', color: 'var(--text-muted)'}}>
              Data akan direkam ke CSV dalam <strong>{formatTime(stats.flush_in_seconds)}</strong>
            </p>
            
            <div className="stats-grid">
              <div className="stat-box">
                <span className="stat-label">Di Frame Saat Ini</span>
                <span className="stat-value">{stats.active_people}</span>
              </div>
              <div className="stat-box">
                <span className="stat-label">Melihat Kamera</span>
                <span className="stat-value stat-highlight">{stats.watching_now}</span>
              </div>
              <div className="stat-box">
                <span className="stat-label">Total Lewat (Sesi)</span>
                <span className="stat-value">{stats.people_passing}</span>
              </div>
              <div className="stat-box">
                <span className="stat-label">Total Melihat (Sesi)</span>
                <span className="stat-value stat-highlight">{stats.people_watching}</span>
              </div>
            </div>

            <div className="log-container">
              {logs.map((log, i) => (
                <div key={i} className="log-entry">{log}</div>
              ))}
              <div ref={logsEndRef} />
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}

export default App
