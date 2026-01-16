# ClarifyVoice Desktop Agent 

##  Setup Complete!

The application is now self-contained with **SoX included**. You do NOT need to install anything else.

##  How to Run

### Option 1: Run the Executable (Recommended)
Go to the `release/win-unpacked` folder and double-click:
**`ClarifyVoice.exe`**

### Option 2: Use the Start Script
Double-click **`start.bat`** in this folder.

##  Build Note
If you run `npm run build`, you might see an error at the end:
`ERROR: Cannot create symbolic link...`
**You can safely IGNORE this error.** The application is successfully built in the `release/win-unpacked` folder before this error occurs.

##  How to Use

1. **Launch the app**. You will see a small floating bar at the top-right.
   - Status: **Ready (Alt+L)**
2. **Press Alt + L** to start recording.
   - The bar will turn **RED** ("Recording...").
3. **Speak your message**.
4. **Press Alt + L** again to stop.
   - The bar will turn **BLUE** ("Processing...").
5. The text will be **automatically pasted** into your active window.

##  Troubleshooting

- **"spawn sox ENOENT" Error**: This is fixed! The app now uses the bundled SoX binary.
- **Build Error**: As mentioned, ignore the "symbolic link" error during build.
