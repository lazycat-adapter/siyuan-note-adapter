import hashlib
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_package import binding, validate_contract, verify
from registry import checked_json, content_hash, download_layer, verify_local

SOURCE = "docker.io/b3log/siyuan:v3.8.10"
PIN = SOURCE + "@sha256:" + "a" * 64
OFFICIAL = "registry.lazycat.cloud/peter/b3log/siyuan:0123456789abcdef"
MANIFEST = """services:
  siyuan:
    # upstream: %s
    image: %s
"""


class PackageAcceptance(unittest.TestCase):
    def test_deploy_contract_matches_published_package(self):
        validate_contract(ROOT)

    def test_workspace_auth_proxy_and_identity_cannot_drift(self):
        for change in (
            lambda doc: doc["services"]["siyuan"].update(
                binds=["/lzcapp/var/new:/siyuan/workspace"]
            ),
            lambda doc: doc["services"]["siyuan"]["environment"].pop(
                "SIYUAN_ACCESS_AUTH_CODE"
            ),
            lambda doc: doc["services"]["siyuan"]["environment"].update(PGID="1001"),
            lambda doc: doc["application"]["upstreams"][0].update(
                remove_this_request_headers=["Origin"]
            ),
            lambda doc: doc["application"].update(multi_instance=False),
        ):
            with self.subTest(
                change=change
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("package.yml", "lzc-deploy-params.yml"):
                    shutil.copyfile(ROOT / name, root / name)
                manifest = yaml.safe_load((ROOT / "lzc-manifest.yml").read_text())
                change(manifest)
                (root / "lzc-manifest.yml").write_text(yaml.safe_dump(manifest))
                with self.assertRaises(ValueError):
                    validate_contract(root)

    def test_binding_requires_official_destination_and_pinned_stable_source(self):
        self.assertEqual(binding(MANIFEST % (PIN, OFFICIAL)), (SOURCE, PIN, OFFICIAL))
        for source, image in (
            (SOURCE, OFFICIAL),
            (PIN.replace("v3.8.10", "latest"), OFFICIAL),
            (PIN.replace("v3.8.10", "v3.8.1"), OFFICIAL),
            (PIN.replace("v3.8.10", "v3.8.10-rc.1"), OFFICIAL),
            (PIN, OFFICIAL.replace(".cloud", ".cloud.example")),
            (PIN, OFFICIAL.replace(":0123456789abcdef", ":latest")),
        ):
            with self.subTest(source=source, image=image), self.assertRaises(
                ValueError
            ):
                binding(MANIFEST % (source, image))
        with self.assertRaises(ValueError):
            binding("    # upstream: " + PIN + "\n" + MANIFEST % (PIN, OFFICIAL))
        with self.assertRaises(ValueError):
            binding(
                MANIFEST.replace(
                    "# upstream: %s\n    image: %s", "image: %s\n    # upstream: %s"
                )
                % (OFFICIAL, PIN)
            )

    def test_delivery_acceptance_rejects_mismatch_and_mutation(self):
        upstream = {"pinned_ref": PIN, "digest": "source", "content_sha256": "same"}
        mirror = {"digest": "mirror", "content_sha256": "same"}
        scenarios = [
            (upstream, mirror, upstream, mirror, None, 1),
            (
                dict(upstream, pinned_ref=PIN[:-1] + "b"),
                mirror,
                upstream,
                mirror,
                "upstream.*digest",
                0,
            ),
            (
                upstream,
                dict(mirror, content_sha256="different"),
                upstream,
                mirror,
                "runtime content",
                0,
            ),
            (upstream, mirror, dict(upstream, digest="changed"), mirror, "changed", 1),
            (upstream, mirror, upstream, dict(mirror, digest="changed"), "changed", 1),
        ]
        for (
            source,
            destination,
            source_after,
            destination_after,
            error,
            runs,
        ) in scenarios:
            with self.subTest(error=error):
                read = Mock(
                    side_effect=[source, destination, source_after, destination_after]
                )
                runtime = Mock()
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        verify(MANIFEST % (PIN, OFFICIAL), read, runtime)
                else:
                    verify(MANIFEST % (PIN, OFFICIAL), read, runtime)
                    runtime.assert_called_once_with("v3.8.10", mirror)
                    self.assertEqual(
                        [call.args[0] for call in read.call_args_list],
                        [SOURCE, OFFICIAL, SOURCE, OFFICIAL],
                    )
                self.assertEqual(runtime.call_count, runs)


class RegistryChecks(unittest.TestCase):
    def test_only_official_provenance_metadata_is_ignored(self):
        src = {
            "os": "linux",
            "architecture": "amd64",
            "rootfs": {"diff_ids": ["a"]},
            "config": {"Env": ["TEST=true"]},
        }
        dst = dict(
            src,
            config=dict(
                src["config"], Labels={"cloud.lazycat.image.origin_hash": "hash"}
            ),
        )
        self.assertEqual(content_hash(src), content_hash(dst))
        dst["config"]["Labels"]["cloud.lazycat.image.unexpected"] = "new"
        self.assertNotEqual(content_hash(src), content_hash(dst))
        self.assertNotEqual(
            content_hash(src), content_hash(dict(src, rootfs={"diff_ids": ["b"]}))
        )

    def test_layer_download_rejects_corrupt_and_wrong_size_responses(self):
        content = b"fixture layer"
        descriptor = {
            "size": len(content),
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "layer"
            with patch("registry.open_url", return_value=io.BytesIO(content)):
                download_layer(descriptor, destination)
                self.assertEqual(destination.read_bytes(), content)
            for invalid in (b"x" * len(content), content[:-1], content + b"x"):
                with self.subTest(length=len(invalid)), patch(
                    "registry.open_url", return_value=io.BytesIO(invalid)
                ):
                    with self.assertRaises(ValueError):
                        download_layer(descriptor, destination)

    def test_bad_registry_bytes_and_local_image_layers_are_rejected(self):
        raw = b"{}"
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        self.assertEqual(checked_json(raw, digest), {})
        with self.assertRaises(ValueError):
            checked_json(b"[]", digest)
        image = {
            "digest": digest,
            "config_digest": digest,
            "config": {"rootfs": {"diff_ids": ["a"]}},
        }
        local = {
            "Id": digest,
            "Os": "linux",
            "Architecture": "amd64",
            "RootFS": {"Layers": ["b"]},
        }
        with self.assertRaisesRegex(ValueError, "filesystem"):
            verify_local(image, local)


if __name__ == "__main__":
    unittest.main()
