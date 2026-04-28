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
      pyProcess.kill()
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

    pyProcess.stdout.on('data', (data) => {
      const lines = data.toString().split('\n')
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

    pyProcess.on('close', (code) => {
      console.log(`Python process exited with code ${code}`)
      BrowserWindow.getAllWindows()[0]?.webContents.send('python-message', { type: 'done', message: `Process exited (${code})` })
    })

    return true
  })

  ipcMain.handle('stop-python', () => {
    if (pyProcess) {
      pyProcess.kill()
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
