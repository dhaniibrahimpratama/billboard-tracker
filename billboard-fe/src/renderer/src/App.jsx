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
