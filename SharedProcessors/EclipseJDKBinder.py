#!/usr/local/autopkg/python
"""See docstring for EclipseJDKBinder class"""

import os
import shutil
import subprocess

from autopkglib import Processor, ProcessorError

__all__ = ["EclipseJDKBinder"]


def run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProcessorError(f"{' '.join(cmd)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def archs(path):
    return run(["/usr/bin/lipo", "-archs", path]).split()


def set_vm(ini_text, java_path):
    """Returns the Eclipse .ini text with any -vm entry replaced by java_path, placed before -vmargs
    (the launcher ignores -vm after -vmargs)."""
    lines = ini_text.splitlines()
    out, skip = [], False
    for line in lines:
        if skip:
            skip = False
            continue
        if line.strip() == "-vm":
            skip = True
            continue
        out.append(line)
    try:
        i = [l.strip() for l in out].index("-vmargs")
    except ValueError:
        i = len(out)
    out[i:i] = ["-vm", java_path]
    return "\n".join(out) + "\n"


class EclipseJDKBinder(Processor):
    """Installs a JDK from a tar.gz into a package root and pins an Eclipse-based app in the same
    package root to it, so the app ignores JAVA_HOME and every other JDK on the Mac.

    The JDK's .jdk bundle is copied to pkgroot + jdk_install_path. Give jdk_install_path a name that
    doesn't end in .jdk (eg /Library/Java/JavaVirtualMachines/zulu-25.jdk-intel) and
    /usr/libexec/java_home won't list it, so other apps and tools never pick it up.

    The app's launcher .ini (Contents/Eclipse/<launcher>.ini) gets '-vm <jdk_install_path>/Contents/
    Home/bin/java' before -vmargs, replacing any existing -vm. Both the JDK's java and the app's
    launcher are checked for required_arch, since the Eclipse launcher loads the JVM in-process and
    the two must match. Editing the .ini breaks the app's signature seal, so the app is re-signed
    ad hoc (resign_app)."""

    description = __doc__
    input_variables = {
        "jdk_archive": {"required": True, "description": "Path to the JDK .tar.gz."},
        "pkgroot": {"required": True, "description": "Package root."},
        "jdk_install_path": {
            "required": True,
            "description": "Where the JDK is installed, eg /Library/Java/JavaVirtualMachines/zulu-25.jdk-intel.",
        },
        "app_path": {
            "required": True,
            "description": "Path of the app inside pkgroot, eg %pkgroot%/Applications/ApacheDirectoryStudio.app.",
        },
        "required_arch": {
            "required": False,
            "default": "x86_64",
            "description": "Architecture the JDK and the app launcher must both support. Defaults to x86_64.",
        },
        "resign_app": {
            "required": False,
            "default": True,
            "description": "Re-sign the app ad hoc after editing its .ini. Defaults to True.",
        },
    }
    output_variables = {
        "jdk_java_path": {"description": "Installed path of the pinned java binary."},
        "jdk_java_version": {"description": "JAVA_VERSION from the JDK's release file."},
        "jdk_implementor_version": {"description": "IMPLEMENTOR_VERSION from the JDK's release file, eg Zulu25.28+85-CA."},
    }

    def find_jdk_bundle(self, work):
        for root, dirs, _ in os.walk(work):
            for d in dirs:
                if d.endswith(".jdk") and os.path.isfile(os.path.join(root, d, "Contents/Home/bin/java")):
                    return os.path.join(root, d)
        raise ProcessorError("No .jdk bundle with Contents/Home/bin/java found in jdk_archive.")

    def find_ini(self, app):
        macos = os.path.join(app, "Contents/MacOS")
        launchers = [f for f in os.listdir(macos) if os.access(os.path.join(macos, f), os.X_OK)]
        if len(launchers) != 1:
            raise ProcessorError(f"Expected one launcher in {macos}, found {launchers}.")
        launcher = os.path.join(macos, launchers[0])
        for d in ("Contents/Eclipse", "Contents/MacOS"):
            ini = os.path.join(app, d, launchers[0] + ".ini")
            if os.path.isfile(ini):
                return launcher, ini
        raise ProcessorError(f"No {launchers[0]}.ini found in {app}.")

    def main(self) -> None:
        arch = self.env.get("required_arch", "x86_64")
        install_path = "/" + self.env["jdk_install_path"].strip("/")
        if install_path.endswith(".jdk"):
            self.output(f"WARNING: {install_path} ends in .jdk, so java_home will list it.")
        pkgroot = self.env["pkgroot"]
        app = self.env["app_path"]
        dest = os.path.join(pkgroot, install_path.lstrip("/"))

        # Unpack the JDK
        work = os.path.join(self.env["RECIPE_CACHE_DIR"], "jdk_unpacked")
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        run(["/usr/bin/tar", "-xzf", self.env["jdk_archive"], "-C", work])
        bundle = self.find_jdk_bundle(work)
        if arch not in archs(os.path.join(bundle, "Contents/Home/bin/java")):
            raise ProcessorError(f"JDK in {self.env['jdk_archive']} isn't {arch}.")

        # Read the JDK's version info
        release = {}
        with open(os.path.join(bundle, "Contents/Home/release")) as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    release[k.strip()] = v.strip().strip('"')

        # Copy it into the package root
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        run(["/usr/bin/ditto", bundle, dest])
        shutil.rmtree(work, ignore_errors=True)
        java = os.path.join(install_path, "Contents/Home/bin/java")

        # Pin the app to it
        launcher, ini = self.find_ini(app)
        if arch not in archs(launcher):
            raise ProcessorError(
                f"App launcher {launcher} is {' '.join(archs(launcher))}, not {arch}; it can't load a {arch} JVM."
            )
        with open(ini) as f:
            text = f.read()
        with open(ini, "w") as f:
            f.write(set_vm(text, java))
        self.output(f"Set -vm {java} in {ini}")

        if str(self.env.get("resign_app", True)).lower() not in ("false", "0", "no"):
            run(["/usr/bin/codesign", "--force", "--sign", "-", app])
            run(["/usr/bin/codesign", "--verify", "--strict", app])
            self.output(f"Re-signed {app} ad hoc")

        self.env["jdk_java_path"] = java
        self.env["jdk_java_version"] = release.get("JAVA_VERSION", "")
        self.env["jdk_implementor_version"] = release.get("IMPLEMENTOR_VERSION", "")
        self.output(f"Bundled {self.env['jdk_implementor_version'] or 'JDK'} at {install_path}")


if __name__ == "__main__":
    PROCESSOR = EclipseJDKBinder()
    PROCESSOR.execute_shell()
