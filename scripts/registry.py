"""Resolve and load digest-verified SiYuan images without registry credentials.

Official mirrors are readable through LazyCat's public CDN. The metadata
checks follow the ones used in wcaqrl/affine-adapter.
"""

import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
CDN = "https://registry-static.lazycat.cloud/docker/registry/v2/"
PROVENANCE_LABELS = {
    "cloud.lazycat.image.maintainer",
    "cloud.lazycat.image.origin_hash",
    "cloud.lazycat.image.origin_image",
}


def open_url(url, headers=None):
    return urlopen(
        Request(
            url,
            headers={
                "User-Agent": "siyuan-lazycat-adapter/1",
                **(headers or {}),
            },
        ),
        timeout=60,
    )


def fetch(url, headers=None):
    limit = 16 * 1024 * 1024
    with open_url(url, headers) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Oversized image metadata")
    return data


def checked_json(raw, digest):
    if (
        not DIGEST.fullmatch(digest)
        or "sha256:" + hashlib.sha256(raw).hexdigest() != digest
    ):
        raise ValueError("Registry content does not match digest")
    return json.loads(raw)


def blob_url(digest):
    if not DIGEST.fullmatch(digest):
        raise ValueError("Invalid blob digest")
    value = digest[7:]
    return f"{CDN}blobs/sha256/{value[:2]}/{value}/data"


def content_hash(config):
    if not config.get("rootfs", {}).get("diff_ids"):
        raise ValueError("Image filesystem identity missing")
    runtime = dict(config["config"])
    labels = {
        k: v
        for k, v in (runtime.pop("Labels", None) or {}).items()
        if k not in PROVENANCE_LABELS
    }
    if labels:
        runtime["Labels"] = labels
    # Creation time and history can change on copy. All effective runtime
    # settings and every uncompressed layer must remain exactly the same.
    content = {key: config[key] for key in ("os", "architecture", "rootfs")}
    content["config"] = runtime
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def docker_hub(repo):
    headers = {"Accept": ACCEPT}

    def get(kind, selector):
        url = f"https://registry-1.docker.io/v2/{repo}/{kind}/{selector}"
        try:
            return fetch(url, headers)
        except HTTPError as error:
            if error.code != 401:
                raise
            fields = dict(
                re.findall(
                    r'(\w+)="([^"]*)"', error.headers.get("WWW-Authenticate", "")
                )
            )
            if fields.get("realm") != "https://auth.docker.io/token":
                raise ValueError(
                    "Unexpected Docker Hub authentication endpoint"
                ) from error
            query = urlencode(
                {"service": "registry.docker.io", "scope": f"repository:{repo}:pull"}
            )
            token = json.loads(fetch(fields["realm"] + "?" + query))
            headers["Authorization"] = "Bearer " + token["token"]
            return fetch(url, headers)

    return get


def inspect_image(reference):
    host, path = reference.split("/", 1)
    name, sep, digest = path.partition("@")
    repo, _, tag = name.partition(":")
    selector = digest if sep else tag
    if not re.fullmatch(r"[a-z0-9_./-]+", repo) or not re.fullmatch(
        r"[a-z0-9._:-]+", selector
    ):
        raise ValueError("Invalid image reference")
    if sep and not DIGEST.fullmatch(digest):
        raise ValueError("Only sha256 OCI digests are supported")
    if host == "registry.lazycat.cloud":
        # A tag resolves through the repository's current immutable manifest link.
        if not sep:
            link = f"{CDN}repositories/{repo}/_manifests/tags/{tag}/current/link"
            digest = fetch(link).decode().strip()

        def get(_kind, value):
            return fetch(blob_url(value))

        raw = get("manifests", digest)
    elif host == "docker.io" and repo == "b3log/siyuan":
        get = docker_hub(repo)
        raw = get("manifests", selector)
        if not sep:
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    else:
        raise ValueError("Unexpected registry or source repository")
    manifest = checked_json(raw, digest)
    if "manifests" in manifest:
        matches = [
            item
            for item in manifest["manifests"]
            if (
                item.get("platform", {}).get("os"),
                item.get("platform", {}).get("architecture"),
            )
            == ("linux", "amd64")
        ]
        if len(matches) != 1:
            raise ValueError("Expected one linux/amd64 manifest")
        digest = matches[0]["digest"]
        raw = get("manifests", digest)
        manifest = checked_json(raw, digest)
    config_digest = manifest["config"]["digest"]
    config_raw = get("blobs", config_digest)
    config = checked_json(config_raw, config_digest)
    if (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
        raise ValueError("Unexpected image platform")
    return {
        "ref": reference,
        "pinned_ref": reference.split("@", 1)[0] + "@" + digest,
        "digest": digest,
        "config_digest": config_digest,
        "content_sha256": content_hash(config),
        "config": config,
        "config_raw": config_raw,
        "manifest": manifest,
        "manifest_raw": raw,
    }


def download_layer(layer, path):
    expected = layer["digest"]
    digest = hashlib.sha256()
    remaining = layer["size"]
    with open_url(blob_url(expected)) as response, path.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("Official layer exceeds manifest size")
            digest.update(chunk)
            output.write(chunk)
    if remaining or "sha256:" + digest.hexdigest() != expected:
        raise ValueError("Official layer does not match its manifest descriptor")


def write_archive(image, tag, directory):
    """Docker archive with verified config and gzip/raw layers from the CDN."""
    archive_path = directory / "image.tar"
    layer_path = directory / "layer"

    def blob_path(digest):
        return "blobs/sha256/" + digest[7:]

    manifest_raw = image["manifest_raw"]
    config_path = blob_path(image["config_digest"])
    layers = image["manifest"]["layers"]
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": image["manifest"]["mediaType"],
                "digest": image["digest"],
                "size": len(manifest_raw),
                "platform": {"os": "linux", "architecture": "amd64"},
                "annotations": {
                    "io.containerd.image.name": "docker.io/" + tag,
                    "org.opencontainers.image.ref.name": tag.rsplit(":", 1)[1],
                },
            }
        ],
    }
    # Modern Docker loads the original OCI manifest byte-for-byte. The
    # manifest.json view lets Docker's classic store load the same config.
    docker_manifest = [
        {
            "Config": config_path,
            "RepoTags": [tag],
            "Layers": [blob_path(layer["digest"]) for layer in layers],
        }
    ]
    files = {
        "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        "index.json": json.dumps(index).encode(),
        "manifest.json": json.dumps(docker_manifest).encode(),
        config_path: image["config_raw"],
        blob_path(image["digest"]): manifest_raw,
    }
    with tarfile.open(archive_path, "w") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        for digest, layer in {layer["digest"]: layer for layer in layers}.items():
            download_layer(layer, layer_path)
            archive.add(layer_path, arcname=blob_path(digest))
    layer_path.unlink(missing_ok=True)
    return archive_path


def verify_local(image, local):
    if local.get("Id") not in (image["digest"], image["config_digest"]) or (
        local.get("Os"),
        local.get("Architecture"),
    ) != ("linux", "amd64"):
        raise ValueError("Pulled image identity/platform differs")
    if local.get("RootFS", {}).get("Layers") != image["config"]["rootfs"]["diff_ids"]:
        raise ValueError("Pulled filesystem differs")
    for key in ("Env", "Cmd", "Entrypoint", "WorkingDir", "User"):
        actual = local["Config"].get(key)
        expected = image["config"]["config"].get(key)
        if key in ("User", "WorkingDir"):
            actual = "" if actual is None else actual
            expected = "" if expected is None else expected
        if actual != expected:
            raise ValueError("Pulled runtime configuration differs: " + key)
    return local["Id"]


def pull(image):
    reference = image["pinned_ref"]
    official = reference.startswith("registry.lazycat.cloud/")
    if official:
        reference = "siyuan-adapter-test/cdn:" + image["digest"][7:]
    for name in (reference, image["digest"], image["config_digest"]):
        found = subprocess.run(
            ["docker", "image", "inspect", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if found.returncode == 0:
            return verify_local(image, json.loads(found.stdout)[0])
    with tempfile.TemporaryDirectory(prefix="siyuan-docker-") as directory:
        if official:
            print(f"Loading digest-verified official image {image['ref']}", flush=True)
            archive = write_archive(image, reference, Path(directory))
            subprocess.run(
                ["docker", "image", "load", "--input", str(archive)],
                check=True,
                timeout=900,
            )
        else:
            # Ignore stale developer registry credentials when pulling public images.
            subprocess.run(
                [
                    "docker",
                    "--config",
                    directory,
                    "pull",
                    "--platform",
                    "linux/amd64",
                    reference,
                ],
                check=True,
                timeout=900,
            )
    local = json.loads(
        subprocess.check_output(["docker", "image", "inspect", reference], timeout=30)
    )[0]
    return verify_local(image, local)
