@echo off
rem Starts SMC: backend (port 8822) and frontend (port 5180) in two windows.
rem The face-capture worker lives inside the backend: it resumes by itself when
rem a capture session is already running, otherwise start it from
rem   Identity > Training & Captures  (or POST /api/faces/captures/worker/start).
setlocal
set "SMC=%~dp0"
start "SMC backend (8822)" /D "%SMC%backend" cmd /k ".venv\Scripts\python.exe -m app.serve"
start "SMC frontend (5180)" /D "%SMC%frontend" cmd /k "npm run dev"
echo.
echo Backend : http://127.0.0.1:8822   (API; takes ~30 s to load the AI models)
echo Frontend: http://localhost:5180
echo.
echo Live camera viewer   : http://localhost:5180/face-recognition
echo Training and Captures: http://localhost:5180/people   (tab "Training & Captures")
echo.
endlocal
