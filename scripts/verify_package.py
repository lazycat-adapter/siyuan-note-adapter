"""Check the deployment contract and exercise the delivered mirror before packing."""

from pathlib import Path
import re

import yaml

from registry import inspect_image
from test_upgrade import verify_upgrade

ROOT = Path(__file__).resolve().parents[1]
SOURCE = re.compile(
    r"(docker\.io/b3log/siyuan:(v[0-9]+\.[0-9]+\.[0-9]+))@sha256:[a-f0-9]{64}"
)
MIRROR = re.compile(
    r"registry\.lazycat\.cloud/[a-z0-9_./-]+(?::[a-f0-9]{16,64}|@sha256:[a-f0-9]{64})"
)


def validate_contract(root):
    package = yaml.safe_load((root / "package.yml").read_text())
    manifest = yaml.safe_load((root / "lzc-manifest.yml").read_text())
    params = yaml.safe_load((root / "lzc-deploy-params.yml").read_text())["params"]
    if (
        package["package"] != "cloud.lazycat.app.siyuan"
        or package["min_os_version"] != "1.5.0"
    ):
        raise ValueError("Unexpected app identity or LPK v2 minimum OS")
    if [(param["id"], param["type"]) for param in params] != [("authcode", "string")]:
        raise ValueError(
            "The published installation parameter must remain authcode (string)"
        )
    if manifest["application"] != {
        "subdomain": "siyuan",
        "multi_instance": True,
        "upstreams": [{"location": "/", "backend": "http://siyuan:6806"}],
    }:
        raise ValueError(
            "The route must preserve the instance host, Origin and workspace identity"
        )
    service = dict(manifest["services"]["siyuan"])
    service.pop("image")
    if service != {
        "binds": ["/lzcapp/var/:/siyuan/workspace"],
        "command": "serve --workspace=/siyuan/workspace/ --port=6806",
        "environment": {
            "TZ": "Asia/Shanghai",
            "PUID": "1001",
            "PGID": "1002",
            "SIYUAN_ACCESS_AUTH_CODE": "{{ .U.authcode }}",
        },
    } or set(manifest["services"]) != {"siyuan"}:
        raise ValueError(
            "Published workspace mount, ownership or authentication contract changed"
        )


def binding(manifest):
    service = re.search(
        r"(?m)^  siyuan:[ \t]*\n((?:^ {4,}[^\n]*\n|^\n)*)", manifest + "\n"
    )
    upstream_comments = re.findall(r"(?m)^ *# upstream: (\S+)[ \t]*$", manifest)
    pair = re.search(
        r"(?m)^    # upstream: (\S+)[ \t]*\n    image: ", service[1] if service else ""
    )
    if (
        len(upstream_comments) != 1
        or not pair
        or not (source := SOURCE.fullmatch(pair[1]))
    ):
        raise ValueError(
            "Missing immutable upstream digest for the stable SiYuan image"
        )
    image = yaml.safe_load(manifest)["services"]["siyuan"]["image"]
    if tuple(map(int, source[2][1:].split("."))) <= (3, 8, 1):
        raise ValueError(
            "The selected upstream must be newer than the published baseline"
        )
    if not MIRROR.fullmatch(image):
        raise ValueError(
            "The build requires a digest-derived official registry image reference"
        )
    return source[1], pair[1], image


def verify(manifest, read_image=inspect_image, runtime=verify_upgrade):
    source_ref, selected, mirror_ref = binding(manifest)
    source = read_image(source_ref)
    if source["pinned_ref"] != selected:
        raise ValueError(
            "Selected upstream digest no longer matches the stable Docker tag"
        )
    mirror = read_image(mirror_ref)
    if source["content_sha256"] != mirror["content_sha256"]:
        raise ValueError("Official copy changed SiYuan filesystem or runtime content")
    tag = source_ref.rsplit(":", 1)[1]
    # A rejected review retry can allocate a newer LPK patch version. The
    # runtime version comes from the pinned source tag, independently of it.
    runtime(tag, mirror)
    for ref, image in ((source_ref, source), (mirror_ref, mirror)):
        if read_image(ref)["digest"] != image["digest"]:
            raise ValueError("Image tag changed during upgrade verification: " + ref)
    print(
        "PASS: source and official image verified, workspace upgrade and restart passed",
        flush=True,
    )


if __name__ == "__main__":
    validate_contract(ROOT)
    verify((ROOT / "lzc-manifest.yml").read_text())
