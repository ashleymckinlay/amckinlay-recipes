#!/usr/local/autopkg/python
"""See docstring for HomebrewBottleDownloader class"""

import hashlib
import json
import os
import ssl
import urllib.request

import certifi

from autopkglib import Processor, ProcessorError

__all__ = ["HomebrewBottleDownloader"]

FORMULA_API = "https://formulae.brew.sh/api/formula/{}.json"
# ghcr.io serves public Homebrew bottles to anonymous clients with this token
GHCR_HEADERS = {"Authorization": "Bearer QQ==", "User-Agent": "AutoPkg"}
# AutoPkg's Python has no CA bundle of its own; use certifi's, as URLDownloaderPython does
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class HomebrewBottleDownloader(Processor):
    """Resolves a Homebrew formula and its runtime dependencies through the formulae.brew.sh
    API, downloads the bottle for bottle_tag (falling back to fallback_tags) of each one, and
    verifies every bottle against the SHA-256 published in the API. Bottles already in
    download_dir with the right checksum are reused. Writes a manifest describing the
    downloaded bottles for HomebrewBottleBundler."""

    description = __doc__
    input_variables = {
        "formula": {"required": True, "description": "Homebrew formula name, eg graphviz."},
        "bottle_tag": {
            "required": True,
            "description": "Bottle tag to download, eg arm64_sequoia. Bottles run on that macOS "
            "version and later.",
        },
        "fallback_tags": {
            "required": False,
            "default": ["all"],
            "description": "Tags to try when a formula has no bottle_tag bottle.",
        },
        "download_dir": {
            "required": False,
            "description": "Where to keep bottles. Defaults to RECIPE_CACHE_DIR/downloads/bottles.",
        },
    }
    output_variables = {
        "version": {"description": "Version of the formula (eg 16.1.0)."},
        "bundle_version": {
            "description": "version plus a short hash of every bottle's SHA-256, so a dependency "
            "update produces a new version too (eg 16.1.0-3f9a2c1d)."
        },
        "bottle_manifest": {"description": "Path to the JSON manifest of downloaded bottles."},
        "bottle_count": {"description": "Number of bottles (formula plus dependencies)."},
        "download_changed": {"description": "True if any bottle was downloaded this run."},
    }

    def fetch_json(self, url):
        try:
            with urllib.request.urlopen(url, context=SSL_CONTEXT) as resp:
                return json.load(resp)
        except Exception as err:  # pylint: disable=broad-except
            raise ProcessorError(f"Can't fetch {url}: {err}")

    def resolve(self, formula):
        """Formula and its runtime dependencies, dependencies first."""
        seen, order = {}, []

        def walk(name):
            if name in seen:
                return
            info = self.fetch_json(FORMULA_API.format(name))
            seen[name] = info
            for dep in info.get("dependencies", []):
                walk(dep)
            order.append(name)

        walk(formula)
        return [seen[n] for n in order]

    @staticmethod
    def sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def main(self) -> None:
        formula = self.env["formula"]
        tags = [self.env["bottle_tag"]] + list(self.env.get("fallback_tags") or ["all"])
        download_dir = self.env.get("download_dir") or os.path.join(
            self.env["RECIPE_CACHE_DIR"], "downloads", "bottles"
        )
        os.makedirs(download_dir, exist_ok=True)

        changed = False
        bottles = []
        for info in self.resolve(formula):
            name = info["name"]
            files = info["bottle"]["stable"]["files"]
            tag = next((t for t in tags if t in files), None)
            if tag is None:
                raise ProcessorError(f"No {'/'.join(tags)} bottle for {name}; available: {', '.join(files)}")
            entry = files[tag]
            version = info["versions"]["stable"]
            path = os.path.join(download_dir, f"{name}--{version}.{tag}.bottle.tar.gz")

            if os.path.exists(path) and self.sha256(path) == entry["sha256"]:
                self.output(f"Using existing {os.path.basename(path)}", verbose_level=2)
            else:
                self.output(f"Downloading {name} {version} ({tag})")
                req = urllib.request.Request(entry["url"], headers=GHCR_HEADERS)
                try:
                    with urllib.request.urlopen(req, context=SSL_CONTEXT) as resp, open(path + ".part", "wb") as out:
                        while True:
                            chunk = resp.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                except Exception as err:  # pylint: disable=broad-except
                    raise ProcessorError(f"Can't download {name} bottle: {err}")
                actual = self.sha256(path + ".part")
                if actual != entry["sha256"]:
                    os.unlink(path + ".part")
                    raise ProcessorError(
                        f"sha256 mismatch for {name} bottle: expected {entry['sha256']}, got {actual}"
                    )
                os.replace(path + ".part", path)
                changed = True

            bottles.append({
                "name": name,
                "version": version,
                "tag": tag,
                "cellar": entry.get("cellar"),
                "sha256": entry["sha256"],
                "path": path,
                "keg_dir": f"{name}/{version}" + (f"_{info['revision']}" if info.get("revision") else ""),
            })

        top = bottles[-1]
        combined = hashlib.sha256("".join(b["sha256"] for b in bottles).encode()).hexdigest()[:8]
        manifest = os.path.join(download_dir, f"{formula}-manifest.json")
        with open(manifest, "w") as f:
            json.dump({"formula": formula, "bottles": bottles}, f, indent=1)

        self.env["version"] = top["version"]
        self.env["bundle_version"] = f"{top['version']}-{combined}"
        self.env["bottle_manifest"] = manifest
        self.env["bottle_count"] = len(bottles)
        self.env["download_changed"] = changed
        self.output(f"{formula} {top['version']} with {len(bottles) - 1} dependencies "
                    f"({'changed' if changed else 'unchanged'}); bundle version {self.env['bundle_version']}")


if __name__ == "__main__":
    PROCESSOR = HomebrewBottleDownloader()
    PROCESSOR.execute_shell()
