#!/usr/local/autopkg/python
"""See docstring for HashVerifier class"""

import hashlib

from autopkglib import Processor, ProcessorError

__all__ = ["HashVerifier"]


class HashVerifier(Processor):
    """Verifies a file against an expected checksum, for downloads that aren't
    code signed. Fetch the publisher's checksum first (eg with URLTextSearcher)
    and pass it in as expected_hash."""

    description = __doc__
    input_variables = {
        "input_path": {
            "required": True,
            "description": "Path to the file to verify.",
        },
        "expected_hash": {
            "required": True,
            "description": "Expected hex digest (case-insensitive).",
        },
        "algorithm": {
            "required": False,
            "default": "sha256",
            "description": "hashlib algorithm name, eg sha256 or sha512. Defaults to sha256.",
        },
    }
    output_variables = {}

    def main(self) -> None:
        path = self.env["input_path"]
        algorithm = self.env.get("algorithm", "sha256")
        expected = self.env["expected_hash"].strip().lower()
        try:
            digest = hashlib.new(algorithm)
        except ValueError as err:
            raise ProcessorError(f"Unsupported algorithm {algorithm}: {err}")
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as err:
            raise ProcessorError(f"Can't read {path}: {err}")
        actual = digest.hexdigest()
        if actual != expected:
            raise ProcessorError(
                f"{algorithm} mismatch for {path}: expected {expected}, got {actual}"
            )
        self.output(f"{algorithm} matches for {path}")


if __name__ == "__main__":
    PROCESSOR = HashVerifier()
    PROCESSOR.execute_shell()
