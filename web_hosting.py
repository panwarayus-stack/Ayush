# web_hosting.py
import os, subprocess, shutil, zipfile
from pathlib import Path
from flask import Flask, request, redirect, url_for, render_template_string

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

processes = {}  # key = filename, value = subprocess.Popen

app = Flask(__name__)

HTML_TEMPLATE = """
<!doctype html>
<title>Hosting Website</title>
<h2>🚀 Hosting Panel</h2>

<form action="{{ url_for('upload') }}" method=post enctype=multipart/form-data>
  <p><input type=file name=file>
     <input type=submit value=Upload>
</form>

<h3>My Files:</h3>
<ul>
{% for f in files %}
  <li>
    {{ f }}
    <a href="{{ url_for('start', filename=f) }}">▶️ Start</a>
    <a href="{{ url_for('stop', filename=f) }}">⏹ Stop</a>
    <a href="{{ url_for('logs', filename=f) }}">📜 Logs</a>
    <a href="{{ url_for('delete', filename=f) }}">❌ Delete</a>
  </li>
{% else %}
  <li>No files uploaded yet.</li>
{% endfor %}
</ul>
"""

@app.route("/", methods=["GET"])
def index():
    files = [p.name for p in UPLOAD_DIR.iterdir() if p.is_file()]
    return render_template_string(HTML_TEMPLATE, files=files)

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return redirect(url_for("index"))
    file = request.files["file"]
    if file.filename == "":
        return redirect(url_for("index"))
    filepath = UPLOAD_DIR / file.filename
    file.save(filepath)
    return redirect(url_for("index"))

@app.route("/start/<filename>")
def start(filename):
    filepath = UPLOAD_DIR / filename
    if not filepath.exists():
        return "File not found", 404
    if filename in processes:
        return f"{filename} already running"
    cmd = ["python3", str(filepath)] if filename.endswith(".py") else None
    if filename.endswith(".js"):
        cmd = ["node", str(filepath)]
    if filename.endswith(".zip"):
        extract_dir = UPLOAD_DIR / (filepath.stem + "_proj")
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        with zipfile.ZipFile(filepath, "r") as z:
            z.extractall(extract_dir)
        main_py = extract_dir / "main.py"
        if main_py.exists():
            cmd = ["python3", str(main_py)]
    if not cmd:
        return "Unsupported file type"
    log_file = open(str(filepath) + ".log", "a", buffering=1)
    p = subprocess.Popen(cmd, cwd=filepath.parent, stdout=log_file, stderr=log_file)
    processes[filename] = p
    return redirect(url_for("index"))

@app.route("/stop/<filename>")
def stop(filename):
    p = processes.get(filename)
    if not p:
        return f"{filename} not running"
    p.terminate()
    processes.pop(filename, None)
    return redirect(url_for("index"))

@app.route("/logs/<filename>")
def logs(filename):
    log_path = UPLOAD_DIR / (filename + ".log")
    if not log_path.exists():
        return "No logs yet"
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        content = "".join(f.readlines()[-500:])
    return f"<pre>{content}</pre>"

@app.route("/delete/<filename>")
def delete(filename):
    stop(filename)
    filepath = UPLOAD_DIR / filename
    if filepath.exists():
        filepath.unlink()
    log_path = UPLOAD_DIR / (filename + ".log")
    if log_path.exists():
        log_path.unlink()
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)