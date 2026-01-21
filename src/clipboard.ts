import { app, clipboard } from 'electron';
import { exec } from 'child_process';
import path from 'path';
import fs from 'fs';
import os from 'os';

/**
 * Paste text to the active window using platform-specific methods
 */
export async function pasteTextToActiveWindow(text: string): Promise<boolean> {
  console.log('Attempting to paste text:', text.substring(0, 100) + (text.length > 100 ? '...' : ''));

  try {
    // Use Electron's native clipboard API
    clipboard.writeText(text);
    console.log('Text copied to clipboard successfully');

    const platform = os.platform();

    if (platform === 'win32') {
      await new Promise<void>((resolve, reject) => {
        // Use VBScript for reliable key sending on Windows
        const vbsScript = `
Set WshShell = CreateObject("WScript.Shell")
WScript.Sleep 200
WshShell.SendKeys "^v"
`;
        const vbsPath = path.join(app.getPath('userData'), 'paste.vbs');
        fs.writeFileSync(vbsPath, vbsScript);

        exec(`cscript //nologo "${vbsPath}"`, (err, _stdout, stderr) => {
          if (err) {
            console.error('Paste exec error:', err);
            console.error('stderr:', stderr);
            reject(err);
          } else {
            console.log('Paste command executed successfully');
            resolve();
          }
        });
      });
    } else if (platform === 'darwin') {
      await new Promise<void>((resolve, reject) => {
        exec('osascript -e \'tell application "System Events" to keystroke "v" using command down\'', (err) => {
          if (err) {
            console.error('Paste failed:', err);
            reject(err);
          } else {
            resolve();
          }
        });
      });
    } else if (platform === 'linux') {
      await new Promise<void>((resolve, reject) => {
        exec('xdotool key ctrl+v', (err) => {
          if (err) {
            console.error('Paste failed:', err);
            reject(err);
          } else {
            resolve();
          }
        });
      });
    }

    return true;
  } catch (err) {
    console.error('Failed to copy/paste:', err);
    return false;
  }
}
