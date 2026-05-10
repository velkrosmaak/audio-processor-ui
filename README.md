# Audio Directory Inspector

A small Flask app for scanning directories of audio files and previewing metadata plus embedded artwork in the browser.

## Features

- Drag and drop a directory into the browser window
- Choose a folder with the browser folder picker
- Analyze a direct filesystem path on the machine running Flask
- Extract common audio metadata with `mutagen`
- Show embedded cover art when present

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

Then open `http://127.0.0.1:5000`.

## Notes

- Folder drag-and-drop and folder picker analysis upload files from the browser to Flask temporarily for scanning.
- Direct path analysis reads from the server filesystem, so the path must exist on the host running the Flask app.
- Supported extensions include `mp3`, `flac`, `m4a`, `ogg`, `opus`, `wav`, `aac`, `aiff`, and a few related formats.
