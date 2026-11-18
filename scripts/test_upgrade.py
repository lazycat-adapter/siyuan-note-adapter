"""Exercise a SiYuan upgrade using only disposable Docker resources."""

import argparse
import base64
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPException
import json
import os
import re
import secrets
import socket
import subprocess
import time

from registry import inspect_image, pull

BASELINE_TAG = "v3.8.1"
BASELINE = (
    "registry.lazycat.cloud/medium/b3log/siyuan@"
    "sha256:a5d2297fc2af1858a9b65ad98b913282bce616a5a70435840adb1cf549c110a6"
)
STABLE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
NOTEBOOKS = "/api/notebook/lsNotebooks"
FIXTURE = b"SiYuan LPK upgrade attachment\n"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def docker(*args, env=None, timeout=60, check=True):
    result = subprocess.run(
        ["docker", *args], env=env, text=True, capture_output=True, timeout=timeout
    )
    if check and result.returncode:
        raise RuntimeError(f"Docker {args[0]} {args[1]}: {result.stderr.strip()}")
    return result.stdout.strip()


class Client:
    def __init__(self, port):
        self.port = port
        self.host = f"127.0.0.1:{port}"
        self.origin = "http://" + self.host
        self.cookie = ""

    def request(self, path, body=None, headers=None, method="POST"):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=8)
        try:
            connection.request(
                method,
                path,
                body,
                {
                    "Host": self.host,
                    "Origin": self.origin,
                    **(headers or {}),
                },
            )
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def api(self, path, data=None, *, token=None, expected=200, ok=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Token " + token
        elif self.cookie:
            headers["Cookie"] = self.cookie
        status, _, body = self.request(path, json.dumps(data or {}), headers)
        require(status == expected, f"{path}: expected HTTP {expected}, got {status}")
        if not ok:
            return
        result = json.loads(body)
        require(result["code"] == 0, f"{path}: returned API code {result['code']}")
        return result.get("data")

    def login(self, code):
        path = "/api/system/loginAuth"
        headers = {"Content-Type": "application/json"}
        self.api(NOTEBOOKS, expected=401, ok=False)
        status, _, body = self.request(
            path, json.dumps({"authCode": "invalid-test-code"}), headers
        )
        require(
            status != 200 or json.loads(body)["code"] != 0,
            "Wrong access code was accepted",
        )
        status, received, body = self.request(
            path, json.dumps({"authCode": code}), headers
        )
        require(
            status == 200 and json.loads(body)["code"] == 0,
            "Existing workspace access code failed",
        )
        self.cookie = received.get("Set-Cookie", "").partition(";")[0]
        require(bool(self.cookie), "No session cookie after login")
        self.api(NOTEBOOKS)

    def websocket(self):
        # A masked WebSocket ping proves the authenticated session stays open
        # after the HTTP upgrade, where SiYuan performs its session check.
        key = base64.b64encode(os.urandom(16)).decode()
        headers = {
            "Host": self.host,
            "Origin": self.origin,
            "Cookie": self.cookie,
            "Connection": "Upgrade",
            "Upgrade": "websocket",
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": key,
        }
        with socket.create_connection(
            ("127.0.0.1", self.port), timeout=8
        ) as conn, conn.makefile("rb") as reader:
            request = (
                "GET /ws HTTP/1.1\r\n"
                + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                + "\r\n"
            )
            conn.sendall(request.encode())
            require(
                reader.readline().split()[1:2] == [b"101"],
                "Same-origin WebSocket upgrade failed",
            )
            while reader.readline() not in (b"\r\n", b""):
                pass
            conn.sendall(b"\x89\x81\x01\x02\x03\x04" + bytes([ord("x") ^ 1]))
            for _ in range(20):
                frame = reader.read(2)
                require(
                    len(frame) == 2 and frame[0] & 0x0F != 8,
                    "Authenticated WebSocket closed",
                )
                length = frame[1] & 0x7F
                if length >= 126:
                    length = int.from_bytes(
                        reader.read(2 if length == 126 else 8), "big"
                    )
                require(
                    length <= 1024 * 1024, "Unexpectedly large WebSocket test frame"
                )
                payload = reader.read(length)
                if frame[0] & 0x0F == 10:
                    require(payload == b"x", "Unexpected WebSocket pong")
                    return
            raise RuntimeError("No authenticated WebSocket pong received")

    def upload(self):
        boundary = "siyuan-test-" + secrets.token_hex(12)
        payload = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="assetsDirPath"\r\n\r\n/assets/\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file[]"; filename="fixture.txt"\r\n'
                "Content-Type: text/plain\r\n\r\n"
            ).encode()
            + FIXTURE
            + f"\r\n--{boundary}--\r\n".encode()
        )
        status, _, body = self.request(
            "/api/asset/upload",
            payload,
            {
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "Cookie": self.cookie,
            },
        )
        require(status == 200, f"Fixture attachment upload failed with HTTP {status}")
        result = json.loads(body)
        require(result["code"] == 0, "Attachment upload failed")
        return result["data"]["succMap"]["fixture.txt"]

    def create_document(self, notebook, name, marker):
        document = self.api(
            "/api/filetree/createDocWithMd",
            {
                "notebook": notebook,
                "path": "/" + name,
                "markdown": marker,
            },
        )
        return document["id"] if isinstance(document, dict) else document

    def reads_document(self, document, marker):
        require(
            marker
            in self.api("/api/export/exportMdContent", {"id": document})["content"],
            "Workspace document content did not persist",
        )

    def notebooks(self, token=None):
        return {entry["id"] for entry in self.api(NOTEBOOKS, token=token)["notebooks"]}


@contextmanager
def workspace(label):
    volume = "siyuan-upgrade-" + secrets.token_hex(10)
    docker("volume", "create", "--label", f"org.lazycat.test={label}", volume)
    try:
        yield volume
    finally:
        docker("volume", "rm", volume)


@contextmanager
def running(image, volume, tag, code, *, legacy=False):
    name = "siyuan-upgrade-" + secrets.token_hex(10)
    auth = {} if legacy else {"SIYUAN_ACCESS_AUTH_CODE": code}
    command = ["serve", "--workspace=/siyuan/workspace/", "--port=6806"]
    if legacy:
        command = [
            "--workspace=/siyuan/workspace/",
            "serve",
            "--port=6806",
            "--accessAuthCode=" + code,
        ]
    try:
        docker(
            "run",
            "-d",
            "--pull",
            "never",
            "--name",
            name,
            "--memory",
            "768m",
            "--cpus",
            "1",
            "--publish",
            "127.0.0.1::6806",
            "--volume",
            f"{volume}:/siyuan/workspace",
            "-e",
            "PUID=1001",
            "-e",
            "PGID=1002",
            "-e",
            "TZ=Asia/Shanghai",
            *[part for key in auth for part in ("-e", key)],
            image,
            *command,
            env={**os.environ, **auth},
        )
        port = int(docker("port", name, "6806/tcp").rsplit(":", 1)[1])
        client = Client(port)
        deadline = time.monotonic() + 150
        while True:
            try:
                status, _, _ = client.request(
                    NOTEBOOKS, b"{}", {"Content-Type": "application/json"}
                )
                if status == 401:
                    client.login(code)
                    break
            except (OSError, HTTPException):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"{tag} did not become ready with authentication enabled"
                )
            if docker("inspect", "--format", "{{.State.Running}}", name) != "true":
                raise RuntimeError(f"{tag} exited during startup")
            time.sleep(1)
        require(
            client.api("/api/system/version") == tag[1:],
            f"Unexpected running SiYuan version for {tag}",
        )
        owner = docker("exec", name, "stat", "-c", "%u:%g", "/siyuan/workspace")
        require(
            owner == "1001:1002",
            "Workspace ownership differs from the published package",
        )
        yield client
    except Exception:
        logs = docker("logs", "--tail", "18", name, check=False)
        if logs:
            print(logs.replace(code, "[redacted]"), flush=True)
        raise
    finally:
        # Force-remove after an ordinary stop, which lets SiYuan flush its state.
        docker("stop", "--time", "20", name, check=False)
        docker("rm", "--force", name, check=False)


def verify_upgrade(tag, candidate):
    old = pull(inspect_image(BASELINE))
    new = pull(candidate)
    access_code = secrets.token_hex(16)
    clipper_token = secrets.token_hex(24)
    old_text = "思源旧版本升级夹具 " + secrets.token_hex(8)
    new_text = "思源升级后写入夹具 " + secrets.token_hex(8)
    with workspace("siyuan-upgrade") as volume:
        print(
            f"Testing published {BASELINE_TAG} → {tag} using a new temporary workspace",
            flush=True,
        )
        with running(old, volume, BASELINE_TAG, access_code, legacy=True) as before:
            notebook = before.api(
                "/api/notebook/createNotebook", {"name": "Upgrade fixture"}
            )["notebook"]["id"]
            asset = before.upload()
            first = before.create_document(
                notebook, "Before", old_text + f"\n\n[attachment]({asset})"
            )
            before.api("/api/system/setAPIToken", {"token": clipper_token})
            require(
                notebook in before.notebooks(clipper_token),
                "Old Web Clipper API token failed",
            )
            before.reads_document(first, old_text)

        def verify_previous(client):
            require(
                notebook in client.notebooks(clipper_token),
                "Original Web Clipper API token failed after upgrade",
            )
            client.reads_document(first, old_text)
            status, _, data = client.request(
                "/" + asset, method="GET", headers={"Cookie": client.cookie}
            )
            require(
                status == 200 and data == FIXTURE,
                "Original attachment did not survive upgrade",
            )
            client.websocket()

        with running(new, volume, tag, access_code) as after:
            verify_previous(after)
            second = after.create_document(notebook, "After", new_text)

        with running(new, volume, tag, access_code) as restarted:
            verify_previous(restarted)
            restarted.reads_document(second, new_text)

    with workspace("siyuan-fresh-install") as fresh:
        # This value would be split by the old entrypoint's CLI argument handling.
        special_code = secrets.token_hex(8) + " test+&:#"
        with running(new, fresh, tag, special_code) as separate:
            require(
                notebook not in separate.notebooks(),
                "Previous instance workspace leaked into new instance",
            )
            separate.websocket()
    print(
        "PASS: access code, Web Clipper token, docs, attachments, WebSocket, restart and workspace isolation",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag", required=True, help="Stable upstream Docker tag, e.g. v3.8.10"
    )
    args = parser.parse_args()
    if not STABLE_TAG.fullmatch(args.tag) or tuple(
        map(int, args.tag[1:].split("."))
    ) <= (3, 8, 1):
        parser.error("tag must be a stable release newer than v3.8.1")
    verify_upgrade(args.tag, inspect_image(f"docker.io/b3log/siyuan:{args.tag}"))
