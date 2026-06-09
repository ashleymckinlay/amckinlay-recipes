#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GoDownloadProcessor - Custom AutoPkg processor for Go language downloads

This processor scrapes the Go download page (https://go.dev/dl/) to find
and extract the download URL for the specified Go version, OS, and architecture.
"""

from __future__ import print_function
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from autopkg.processor import Processor
from autopkg.common import log_err, log

class GoDownloadProcessor(Processor):
    """Processor to download Go language binaries from https://go.dev/dl/"""
    
    description = "Fetches download URL for Go language from go.dev/dl/"
    input_variables = {
        "url": {
            "required": True,
            "description": "URL of the Go downloads page"
        },
        "os_version": {
            "required": False,
            "description": "OS version (darwin for macOS, linux, windows)",
            "default": "darwin"
        },
        "arch": {
            "required": False,
            "description": "Architecture (amd64, arm64, or 'universal' for current system)",
            "default": "universal"
        },
        "download_both": {
            "required": False,
            "description": "Download both amd64 and arm64 (for universal packages)",
            "default": False
        }
    }
    output_variables = {
        "go_url": {
            "description": "The direct download URL for the Go binary"
        },
        "go_url_amd64": {
            "description": "The download URL for amd64 binary (if download_both=True)"
        },
        "go_url_arm64": {
            "description": "The download URL for arm64 binary (if download_both=True)"
        },
        "go_version": {
            "description": "The version of Go being downloaded"
        },
        "filename": {
            "description": "The filename of the Go binary"
        },
        "filename_amd64": {
            "description": "The filename for amd64 binary (if download_both=True)"
        },
        "filename_arm64": {
            "description": "The filename for arm64 binary (if download_both=True)"
        }
    }
    
    def main(self):
        """Main processor execution"""
        try:
            url = self.env.get("url")
            os_version = self.env.get("os_version", "darwin")
            arch = self.env.get("arch", "universal")
            download_both = self.env.get("download_both", False)
            
            # Auto-detect architecture if universal
            if arch.lower() == "universal":
                arch = self._detect_current_arch()
                self.output(f"Detected current architecture: {arch}")
            
            # Fetch the downloads page
            response = urllib.request.urlopen(url)
            html_content = response.read().decode('utf-8')
            
            if download_both:
                # Download both architectures
                download_url_amd64, version, filename_amd64 = self._parse_download_page(
                    html_content, os_version, "amd64"
                )
                download_url_arm64, _, filename_arm64 = self._parse_download_page(
                    html_content, os_version, "arm64"
                )
                
                if not download_url_amd64 or not download_url_arm64:
                    raise Exception("Could not find both amd64 and arm64 downloads")
                
                self.env["go_url_amd64"] = download_url_amd64
                self.env["go_url_arm64"] = download_url_arm64
                self.env["go_version"] = version
                self.env["filename_amd64"] = filename_amd64
                self.env["filename_arm64"] = filename_arm64
                
                self.output(f"Found Go {version} (universal - both architectures)")
                self.output(f"amd64: {download_url_amd64}")
                self.output(f"arm64: {download_url_arm64}")
            else:
                # Download single architecture
                download_url, version, filename = self._parse_download_page(
                    html_content, os_version, arch
                )
                
                if not download_url:
                    raise Exception(
                        f"Could not find Go download for {os_version}/{arch}"
                    )
                
                self.env["go_url"] = download_url
                self.env["go_version"] = version
                self.env["filename"] = filename
                
                self.output(f"Found Go {version} for {os_version}/{arch}")
                self.output(f"Download URL: {download_url}")
            
        except Exception as error:
            log_err(f"Error in GoDownloadProcessor: {error}")
            raise
    
    def _detect_current_arch(self):
        """Detect current system architecture"""
        import platform
        machine = platform.machine().lower()
        
        # Map system architecture to Go architecture
        arch_map = {
            "x86_64": "amd64",
            "amd64": "amd64",
            "arm64": "arm64",
            "aarch64": "arm64",
            "m1": "arm64"
        }
        
        detected = arch_map.get(machine, "amd64")
        return detected
    
    def _parse_download_page(self, html, os_version, arch):
        """
        Parse the Go downloads page to extract version and URL
        
        Returns:
            tuple: (download_url, version, filename) or (None, None, None)
        """
        # Map our os/arch to Go's naming convention
        os_map = {
            "darwin": "darwin",
            "macos": "darwin",
            "linux": "linux",
            "windows": "windows"
        }
        
        arch_map = {
            "amd64": "amd64",
            "arm64": "arm64",
            "x86_64": "amd64"
        }
        
        go_os = os_map.get(os_version.lower(), os_version)
        go_arch = arch_map.get(arch.lower(), arch)
        
        # Pattern to match download links
        # Looking for patterns like: go1.21.5.darwin-amd64.tar.gz
        pattern = (
            rf'href="([^"]*?go\d+\.\d+(?:\.\d+)?\.{go_os}-{go_arch}\.tar\.gz)"'
        )
        
        matches = re.findall(pattern, html)
        
        if not matches:
            return None, None, None
        
        # Get the first match (should be latest)
        download_path = matches[0]
        
        # Handle relative and absolute URLs
        if download_path.startswith('http'):
            download_url = download_path
        else:
            download_url = urllib.parse.urljoin("https://go.dev/dl/", download_path)
        
        # Extract filename and version
        filename = os.path.basename(download_path)
        
        # Extract version number
        version_match = re.search(r'go(\d+\.\d+(?:\.\d+)?)', filename)
        version = version_match.group(1) if version_match else "unknown"
        
        return download_url, version, filename


if __name__ == '__main__':
    processor = GoDownloadProcessor()
    processor.execute_shell = lambda cmd: os.system(cmd)
    processor.main()
