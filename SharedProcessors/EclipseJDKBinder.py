#!/usr/local/autopkg/python
"""See docstring for EclipseJDKBinder class"""

import os
import plistlib
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


def set_vm_args(args, java_path, at_end):
    """Returns the launcher argument list with any -vm entry replaced by java_path, placed before
    -vmargs (the launcher ignores -vm after -vmargs), or at the end/start when there's no -vmargs."""
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg.strip() == "-vm":
            skip = True
            continue
        out.append(arg)
    try:
        i = [a.strip() for a in out].index("-vmargs")
    except ValueError:
        i = len(out) if at_end else 0
    out[i:i] = ["-vm", java_path]
    return out


def set_vm(ini_text, java_path):
    """Returns the Eclipse .ini text with -vm set to java_path."""
    return "\n".join(set_vm_args(ini_text.splitlines(), java_path, at_end=True)) + "\n"


def set_plist_vm(plist_path, java_path):
    """Sets -vm java_path in the 'Eclipse' argument array of an app's Info.plist, which the Eclipse
    launcher reads before the .ini. Keeps the plist's format (XML or binary)."""
    with open(plist_path, "rb") as f:
        data = f.read()
    fmt = plistlib.FMT_BINARY if data.startswith(b"bplist") else plistlib.FMT_XML
    info = plistlib.loads(data)
    args = info.get("Eclipse", [])
    if not isinstance(args, list):
        raise ProcessorError(f"'Eclipse' in {plist_path} isn't an array.")
    info["Eclipse"] = set_vm_args(args, java_path, at_end=False)
    with open(plist_path, "wb") as f:
        plistlib.dump(info, f, fmt=fmt)
    return info["Eclipse"]


class EclipseJDKBinder(Processor):
    """Installs a JDK from a tar.gz into a package root and pins an Eclipse-based app in the same
    package root to it, so the app ignores JAVA_HOME and every other JDK on the Mac.

    The JDK's .jdk bundle is copied to pkgroot + jdk_install_path. Give jdk_install_path a name that
    doesn't end in .jdk (eg /Library/Java/JavaVirtualMachines/zulu-25.jdk-intel) and
    /usr/libexec/java_home won't list it, so other apps and tools never pick it up.

    The app's launcher .ini (Contents/Eclipse/<launcher>.ini) and the 'Eclipse' argument array in its
    Contents/Info.plist both get '-vm <jdk_install_path>/Contents/Home/bin/java' before -vmargs,
    replacing any existing -vm. Both the JDK's java and the app's
    launcher are checked for required_arch, since the Eclipse launcher loads the JVM in-process and
    the two must match. Editing these breaks the app's signature seal, so the app is re-signed
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
            "description": "Re-sign the app ad hoc after editing its .ini and Info.plist. Defaults to True.",
        },
    }
    output_variables = {
        "jdk_java_path": {"description": "Installed path of the pinned java binary."},
        "jdk_java_version": {"description": "JAVA_VERSION from the JDK's release file."},
        "jdk_implementor_version": {"description": "IMPLEMENTOR_VERSION from the JDK's release file, eg Zulu25.28+85-CA."},
    }

    def find_jdk_home(self, work):
        """Returns the real path of the JDK home (the directory holding bin/java and release) in the
        unpacked archive. Symlinked directories are checked too, but not walked into."""
        found = []
        for root, dirs, _ in os.walk(work):
            for d in [root] + [os.path.join(root, x) for x in dirs]:
                if os.path.isfile(os.path.join(d, "bin/java")) and os.path.isfile(os.path.join(d, "release")):
                    found.append(os.path.realpath(d))
        if not found:
            listing = []
            for root, dirs, files in os.walk(work):
                depth = root[len(work):].count(os.sep)
                if depth > 2:
                    continue
                for x in sorted(dirs + files):
                    p = os.path.join(root, x)
                    kind = "-> " + os.readlink(p) if os.path.islink(p) else ("/" if os.path.isdir(p) else "")
                    listing.append(f"  {os.path.relpath(p, work)}{kind}")
            raise ProcessorError(
                "No JDK (bin/java with a release file) found in jdk_archive. Top of archive:\n"
                + "\n".join(listing[:60])
            )
        # Prefer a home inside a .jdk bundle
        found.sort(key=lambda h: (not h.endswith(".jdk/Contents/Home"), len(h)))
        return found[0]

    def stage_bundle(self, home, work):
        """Returns a .jdk bundle directory for the JDK home: the archive's own bundle if the home is
        inside one, otherwise a new bundle in work with the home copied to Contents/Home."""
        if home.endswith(".jdk/Contents/Home"):
            return os.path.dirname(os.path.dirname(home))
        bundle = os.path.join(work, "staged.jdk")
        os.makedirs(os.path.join(bundle, "Contents"))
        home_dest = os.path.join(bundle, "Contents/Home")
        run(["/usr/bin/ditto", home, home_dest])
        # Drop any .jdk entries a flat layout links back to itself
        for x in os.listdir(home_dest):
            if x.endswith(".jdk"):
                p = os.path.join(home_dest, x)
                os.unlink(p) if os.path.islink(p) else shutil.rmtree(p)
        self.output("JDK archive has no .jdk bundle; wrapped its home in one")
        return bundle

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
        with open(self.env["jdk_archive"], "rb") as f:
            if f.read(2) != b"\x1f\x8b":
                raise ProcessorError(
                    f"{self.env['jdk_archive']} isn't a gzip file; delete it so it's downloaded again."
                )
        run(["/usr/bin/tar", "-xzf", self.env["jdk_archive"], "-C", work])
        home = self.find_jdk_home(work)
        self.output(f"Found JDK home {os.path.relpath(home, work)}")
        bundle = self.stage_bundle(home, work)
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
        plist = os.path.join(app, "Contents/Info.plist")
        eclipse_args = set_plist_vm(plist, java)
        self.output(f"Set -vm {java} in {plist} (Eclipse: {' '.join(eclipse_args)})")

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
