"""Start MongoDB (Windows service), Flask and Streamlit with one command."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import webbrowser

from pymongo import MongoClient
from pymongo.uri_parser import parse_uri

ROOT = Path(__file__).resolve().parent


def mongo_ready(uri):
    try:
        with MongoClient(uri, serverSelectionTimeoutMS=1500, connectTimeoutMS=1500) as client:
            client.admin.command('ping')
        return True
    except Exception:
        return False


def ensure_mongodb(uri):
    if mongo_ready(uri):
        print('MongoDB is ready.', flush=True)
        return
    try:
        nodes = parse_uri(uri)['nodelist']
    except Exception:
        raise RuntimeError('MONGODB_URI is invalid. Check your connection settings.') from None
    local = len(nodes) == 1 and nodes[0][0] in ('localhost', '127.0.0.1', '::1') and nodes[0][1] == 27017
    if os.name != 'nt' or not local:
        raise RuntimeError('MongoDB is unreachable. Start your MongoDB server and check MONGODB_URI, then retry.')
    print('Trying to start the MongoDB Windows service...', flush=True)
    result = subprocess.run(['sc.exe', 'start', 'MongoDB'], capture_output=True, text=True)
    # 1056 means the service is already running; allow it time to become ready.
    if result.returncode not in (0, 1056):
        if result.returncode == 5:
            raise RuntimeError('Windows requires administrator permission to start MongoDB. Run Start-Service MongoDB once in an administrator PowerShell, then retry this launcher normally.')
        if result.returncode == 1060:
            raise RuntimeError('The MongoDB Windows service is not installed. Install/configure MongoDB, then retry.')
        raise RuntimeError(f'MongoDB service could not start (Windows error {result.returncode}). Check the service and its logs.')
    for _ in range(20):
        if mongo_ready(uri):
            print('MongoDB is ready.', flush=True)
            return
        time.sleep(0.5)
    raise RuntimeError('MongoDB did not become ready. Check its logs and your connection settings.')


def check_ports():
    for port in (8080, 8501):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
            except OSError:
                raise RuntimeError(f'Port {port} is already in use. Stop the previous app using that port and retry.') from None


def wait_for_service(process, url, timeout=45):
    import requests
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('An app process exited during startup. See its terminal output above.')
        try:
            response = requests.get(url, timeout=1)
            if response.status_code < 500:
                return
        except requests.RequestException:
            pass
        time.sleep(0.3)
    raise RuntimeError(f'Timed out waiting for {url}. See the terminal output above.')


def stop_children(children):
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def main():
    children = []
    try:
        check_ports()
        ensure_mongodb(os.environ.get('MONGODB_URI', 'mongodb://127.0.0.1:27017/'))
        commands = [
            [sys.executable, '-m', 'flask', '--app', 'API:app', '--no-debug', 'run', '--host', '127.0.0.1', '--port', '8080'],
            [sys.executable, '-m', 'streamlit', 'run', 'streamlit_app.py', '--server.address', '127.0.0.1', '--server.port', '8501', '--server.headless', 'true'],
        ]
        for command, url in zip(commands, ('http://127.0.0.1:8080/', 'http://127.0.0.1:8501/_stcore/health')):
            child = subprocess.Popen(command, cwd=ROOT)
            children.append(child)
            wait_for_service(child, url)
        print('\nScroopy-Luu is ready: http://127.0.0.1:8501\nPress Ctrl+C to stop Flask and Streamlit. MongoDB stays running.', flush=True)
        webbrowser.open('http://127.0.0.1:8501')
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        raise RuntimeError('An app process stopped. See its output above; shutting down the other process.')
    except KeyboardInterrupt:
        print('\nStopping Scroopy-Luu...', flush=True)
        return 0
    except (RuntimeError, OSError) as exc:
        print(f'\nCould not start Scroopy-Luu: {exc}', file=sys.stderr)
        return 1
    finally:
        stop_children(children)


if __name__ == '__main__':
    raise SystemExit(main())
