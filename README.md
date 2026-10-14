# CRT_Filter
Green black, hacker style filter for desktop.

Drag a rectangle anywhere on screen and everything inside it is shown live as black and phosphor green. Bright pixels become green and dark pixels become black.

Requires Windows 10 2004 or later and Python 3.10 or later.

## Run
Double-click `run.bat`. It installs the dependencies on first run, then starts the app.

Or run it manually:
```
pip install -r requirements.txt
python crt_filter.py
```

## Use
On start, drag to select an area, or press **F** for fullscreen. **Esc** cancels.

| Input | Action |
|---|---|
| Drag window | Move it (the filter follows) |
| Drag edges/corners | Resize |
| Double-click | Toggle fullscreen |
| Right-click | Menu with every option |
| Mouse wheel / Up / Down | Threshold |
| Ctrl + wheel | Contrast (soft mode) |
| M | Binary or soft contrast mode |
| I / S / A | Invert / scanlines / auto threshold |
| 1–9, 0 | Color: green, blue, red, purple, yellow, cyan, amber, pink, orange, white |
| P | Screenshot |
| H | Help overlay |
| Esc | Quit (in fullscreen: exit fullscreen) |

Global hotkeys work even when the window isn't focused:

| Hotkey | Action |
|---|---|
| Ctrl+Alt+F | Fullscreen on/off |
| Ctrl+Alt+R | Reselect area |
| Ctrl+Alt+C | Click-through on/off |
| Ctrl+Alt+ + / - | Threshold |
| Ctrl+Alt+I | Invert |
| Ctrl+Alt+1–9, 0 | Color (same order as above) |
| Ctrl+Alt+P | Screenshot of the filtered view, saved as PNG to `Pictures\CRT_Filter` |
| Ctrl+Alt+Q | Quit |

Fullscreen is click-through, so you can keep using the PC underneath it.

## How it works
The filter window uses `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`, which hides it from screen capture. It captures the pixels underneath itself with `mss`, converts them to grayscale, applies a threshold, and draws the result with a green palette. DRM-protected video (such as Netflix) captures as black.
