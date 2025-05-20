"""
TSserver.py – Now integrates with ScreenShare + studentId mapping
"""

import os
import base64
import glob
import pexpect
import tempfile
import threading
import shlex
import shutil
import re
import random
import string

import eventlet
eventlet.monkey_patch()

from flask import Flask, request
from flask_cors import CORS
from flask_socketio import SocketIO, join_room, leave_room

try:
    from PIL import Image
    PIL_ENABLED = True
except ImportError:
    PIL_ENABLED = False

# -------------------------------------------------
# 1) Flask ‑ App setup
# -------------------------------------------------
app = Flask(__name__)
CORS(app)
app.config["SECRET_KEY"] = "some_secret_key"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

@socketio.on("*")
def _catch_all(event, data=None):
    #print("### caught event:", event)
    print("### caught", event, "on NS =", request.namespace)   # <− add

# Import screen‑share events (handlers live inside ScreenShare.py)
#import ScreenShare  # noqa: E402

# -------------------------------------------------
# 2) Data structures & constants
# -------------------------------------------------
rooms_data: dict[str, dict] = {}
# Example:
# {
#     "ABC123": {
#         "teacherSocketId": "...",
#         "participants": set([...]),
#         "taskText": "",
#         "timeLimit": 0,
#         "examEnded": False,
#         "submittedUsers": set(),
#         "studentSockets": {"studentId": "socketId"}
#     }
# }

ephemeral_sessions: dict[str, dict] = {}

LANG_EXTENSIONS = {
    "python": "py",
    "c": "c",
    "cpp": "cpp",
    "java": "java",
    "js": "js",
    "php": "php",
    "sql": "sql",
}

LANG_COMMANDS = {
    "python": "python3 -u user_code.py",
    "c": "gcc -fdiagnostics-color=never user_code.c -o main && ./main",
    "cpp": "g++ -fdiagnostics-color=never user_code.cpp -o main && ./main",
    "java": "",
    "js": "node user_code.js",
    "php": "php user_code.php",
    "sql": "",
}

# -------------------------------------------------
# 3) Helper utilities
# -------------------------------------------------

def generate_room_code() -> str:
    """Return a 6‑char alphanumeric room code."""
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))


def find_public_class_name(java_code: str) -> str | None:
    """Extract the public class name from Java code."""
    match = re.search(r"public\s+class\s+([A-Za-z_]\w*)", java_code)
    return match.group(1) if match else None


@app.route("/")
def index():
    return "Multi-language runner + Enhanced exam logic"

# -------------------------------------------------
# 4) Exam‑room socket events
# -------------------------------------------------

@socketio.on("create_room")
def handle_create_room():
    code = generate_room_code()
    rooms_data[code] = {
        "teacherSocketId": request.sid,
        "participants": set(),
        "taskText": "",
        "timeLimit": 0,
        "examEnded": False,
        "submittedUsers": set(),
        "studentSockets": {},
    }
    join_room(code)
    socketio.emit("room_created", {"roomCode": code}, room=request.sid)


@socketio.on("send_task")
def handle_send_task(data):
    room = data.get("roomCode")
    task_text = data.get("taskText", "")
    time_limit = data.get("timeLimit", 0)
    if room not in rooms_data:
        socketio.emit("session_error", {"error": f"Room {room} not found"}, room=request.sid)
        return
    info = rooms_data[room]
    info.update({"taskText": task_text, "timeLimit": time_limit, "examEnded": False})
    info["submittedUsers"].clear()
    socketio.emit("new_task", {"taskText": task_text, "timeLimit": time_limit}, room=room)


@socketio.on("end_exam")
def handle_end_exam(data):
    room = data.get("roomCode")
    if room and room in rooms_data:
        rooms_data[room]["examEnded"] = True
        socketio.emit("exam_ended", {}, room=room)


@socketio.on("close_room")
def handle_close_room(data):
    room = data.get("roomCode")
    if room and room in rooms_data:
        socketio.emit("room_closed", {}, room=room)
        rooms_data.pop(room, None)


@socketio.on("join_room")
def handle_join_room(data):
    room = data.get("roomCode")
    student_name = data.get("name", "Unknown")
    student_id = data.get("studentId")
    if room not in rooms_data:
        socketio.emit("session_error", {"error": f"Room {room} not found"}, room=request.sid)
        return
    join_room(room)
    info = rooms_data[room]
    info["participants"].add(request.sid)
    if student_id:
        info["studentSockets"][student_id] = request.sid
    socketio.emit("student_joined", {"studentName": student_name}, room=room)


@socketio.on("submit_solution")
def handle_submit_solution(data):
    room = data.get("roomCode")
    student_name = data.get("name", "Unknown")
    code = data.get("code", "").rstrip()
    language = data.get("language", "")
    task_id = data.get("taskId")

    if room not in rooms_data:
        socketio.emit("session_error", {"error": f"Room {room} not found"}, room=request.sid)
        return
    info = rooms_data[room]
    if info["examEnded"]:
        socketio.emit("session_error", {"error": "Exam ended. No more submissions."}, room=request.sid)
        return
    if request.sid in info["submittedUsers"]:
        return  # ignore resubmissions
    info["submittedUsers"].add(request.sid)

    socketio.emit(
        "solution_submitted",
        {"studentName": student_name, "code": code, "language": language, "taskId": task_id},
        room=room,
    )

@socketio.on("reconnect_teacher")
def handle_reconnect_teacher(data):
    room = data.get("roomCode")
    if room in rooms_data:
        rooms_data[room]["teacherSocketId"] = request.sid


# -------------------------------------------------
# 5) Ephemeral execution (per‑user sessions)
# -------------------------------------------------

@socketio.on("start_session")
def start_session(data):
    sid = request.sid
    code = data.get("code", "").strip()
    language = data.get("language", "python").lower()

    if not code:
        socketio.emit("session_error", {"error": "No code provided"}, room=sid)
        return
    if language not in LANG_EXTENSIONS:
        socketio.emit("session_error", {"error": f"Unsupported language: {language}"}, room=sid)
        return

    cleanup_ephemeral_session(sid)

    session = {
        "child": None,
        "temp_dir": None,
        "sql_temp_dir": None,
        "temp_file": None,
        "thread": None,
        "closing": False,
        "sent_images": set(),
    }
    ephemeral_sessions[sid] = session

    # ---------- non‑SQL ----------
    if language != "sql":
        tmp_dir = tempfile.mkdtemp(prefix="user_session_")
        session["temp_dir"] = tmp_dir
        ext = LANG_EXTENSIONS[language]
        code_file = f"user_code.{ext}"
        code_path = os.path.join(tmp_dir, code_file)

        run_cmd = LANG_COMMANDS[language]
        if language == "java":
            cname = find_public_class_name(code)
            if cname:
                code_file = f"{cname}.java"
                code_path = os.path.join(tmp_dir, code_file)
                run_cmd = f"javac {shlex.quote(code_file)} && java {cname}"
            else:
                run_cmd = "javac user_code.java && java user_code"

        with open(code_path, "w", encoding="utf-8") as f:
            f.write(code)

        shell_cmd = f"cd {shlex.quote(tmp_dir)} && env TERM=dumb {run_cmd}"
        try:
            child = pexpect.spawn("/bin/bash", ["-c", shell_cmd], encoding="utf-8", echo=False)
        except Exception as e:
            socketio.emit("session_error", {"error": str(e)}, room=sid)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            ephemeral_sessions.pop(sid, None)
            return

        session.update({"child": child, "temp_file": code_path})

        def read_output():
            try:
                while not session["closing"] and child.isalive():
                    try:
                        chunk = child.read_nonblocking(size=1, timeout=0.1)
                        if chunk:
                            socketio.emit("python_output", {"data": chunk}, room=sid)
                    except pexpect.exceptions.TIMEOUT:
                        pass
                    except pexpect.exceptions.EOF:
                        break
            except Exception as e:
                socketio.emit("session_error", {"error": str(e)}, room=sid)

            if not session["closing"]:
                try:
                    leftover = child.read()
                    if leftover:
                        socketio.emit("python_output", {"data": leftover}, room=sid)
                except Exception:
                    pass
                scan_for_new_images(sid)
                socketio.emit("process_ended", {}, room=sid)

            cleanup_ephemeral_session(sid)

        t = threading.Thread(target=read_output, daemon=True)
        session["thread"] = t
        t.start()

        socketio.emit("session_started", {}, room=sid)
        return

    # ---------- SQL ----------
    tmp_dir = tempfile.mkdtemp(prefix="sql_session_")
    session["sql_temp_dir"] = tmp_dir

    prepop = os.path.join(os.path.dirname(__file__), "prepopulate.sql")
    if os.path.exists(prepop):
        os.system(f"cd {shlex.quote(tmp_dir)} && sqlite3 ephemeral.db < {shlex.quote(prepop)}")

    code_path = os.path.join(tmp_dir, "user_code.sql")
    with open(code_path, "w", encoding="utf-8") as f:
        f.write(code)

    shell_cmd = f"cd {shlex.quote(tmp_dir)} && env TERM=dumb sqlite3 -header -column ephemeral.db < user_code.sql"
    try:
        child = pexpect.spawn("/bin/bash", ["-c", shell_cmd], encoding="utf-8", echo=False)
    except Exception as e:
        socketio.emit("session_error", {"error": str(e)}, room=sid)
        ephemeral_sessions.pop(sid, None)
        return

    session.update({"child": child, "temp_file": code_path})

    def read_sql_output():
        try:
            while not session["closing"] and child.isalive():
                try:
                    chunk = child.read_nonblocking(size=1, timeout=0.1)
                    if chunk:
                        socketio.emit("python_output", {"data": chunk}, room=sid)
                except pexpect.exceptions.TIMEOUT:
                    pass
                except pexpect.exceptions.EOF:
                    break
        except Exception as e:
            socketio.emit("session_error", {"error": str(e)}, room=sid)

        if not session["closing"]:
            try:
                leftover = child.read()
                if leftover:
                    socketio.emit("python_output", {"data": leftover}, room=sid)
            except Exception:
                pass
            scan_for_new_images(sid)
            socketio.emit("process_ended", {}, room=sid)
        cleanup_ephemeral_session(sid)

    t = threading.Thread(target=read_sql_output, daemon=True)
    session["thread"] = t
    t.start()
    socketio.emit("session_started", {}, room=sid)


@socketio.on("send_input")
def handle_send_input(data):
    sid = request.sid
    session = ephemeral_sessions.get(sid)
    if not session:
        socketio.emit("python_output", {"data": "[No active session]\n"}, room=sid)
        socketio.emit("process_ended", {}, room=sid)
        return
    if session["closing"]:
        socketio.emit("python_output", {"data": "[Session closed]\n"}, room=sid)
        socketio.emit("process_ended", {}, room=sid)
        cleanup_ephemeral_session(sid)
        return
    child = session["child"]
    if not child or not child.isalive():
        socketio.emit("python_output", {"data": "[No active session]\n"}, room=sid)
        socketio.emit("process_ended", {}, room=sid)
        cleanup_ephemeral_session(sid)
        return
    child.sendline(data.get("line", ""))


@socketio.on("disconnect_session")
def handle_disconnect_session():
    sid = request.sid
    session = ephemeral_sessions.get(sid)
    if session and not session["closing"]:
        socketio.emit("python_output", {"data": "[Session killed by user]\n"}, room=sid)
        cleanup_ephemeral_session(sid)
        socketio.emit("process_ended", {}, room=sid)

# -------------------------------------------------
# 6) Plot handling and cleanup helpers
# -------------------------------------------------

def scan_for_new_images(sid):
    session = ephemeral_sessions.get(sid)
    if not session:
        return
    tmp = session.get("temp_dir")
    if not tmp or not os.path.isdir(tmp):
        return
    for pattern in ("*.png", "*.jpg", "*.jpeg"):
        for path in glob.glob(os.path.join(tmp, pattern)):
            if path not in session["sent_images"]:
                handle_plot_file(sid, path)


def handle_plot_file(sid, path):
    session = ephemeral_sessions.get(sid)
    if not session or not os.path.exists(path):
        socketio.emit("session_error", {"error": f"Plot file not found: {path}"}, room=sid)
        return
    try:
        if PIL_ENABLED:
            img = Image.open(path)
            if max(img.size) > 800:
                img.thumbnail((800, 800))
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            data = buf.read()
        else:
            with open(path, "rb") as f:
                data = f.read()
        b64 = base64.b64encode(data).decode()
        socketio.emit("plot_image", {"filename": os.path.basename(path), "image_base64": b64}, room=sid)
        session["sent_images"].add(path)
    except Exception as e:
        socketio.emit("session_error", {"error": f"Could not handle plot file {path}: {e}"}, room=sid)


def cleanup_ephemeral_session(sid):
    session = ephemeral_sessions.get(sid)
    if not session or session["closing"]:
        return
    session["closing"] = True
    child = session.get("child")
    if child and child.isalive():
        child.terminate(force=True)
    for key in ("temp_dir", "sql_temp_dir"):
        tmp = session.get(key)
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
    ephemeral_sessions.pop(sid, None)

# -------------------------------------------------
# 7) Entrypoint
# -------------------------------------------------
if __name__ == "__main__":
    import ScreenShare
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
