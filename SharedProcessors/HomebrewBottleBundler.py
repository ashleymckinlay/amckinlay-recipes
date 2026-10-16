#!/usr/local/autopkg/python
"""See docstring for HomebrewBottleBundler class"""

import json
import os
import shutil
import subprocess

from autopkglib import Processor, ProcessorError

__all__ = ["HomebrewBottleBundler"]

MACHO_MAGICS = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
SYSTEM_PREFIXES = ("/usr/lib/", "/System/")


def is_macho(path):
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    with open(path, "rb") as f:
        return f.read(4) in MACHO_MAGICS


def run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProcessorError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.stdout


class HomebrewBottleBundler(Processor):
    """Builds a self-contained install of a Homebrew formula from the bottles that
    HomebrewBottleDownloader fetched, for packaging outside Homebrew.

    The formula's own keg (bin, lib, share) is copied to pkgroot + install_prefix, with its
    executables moved to libexec and replaced in bin by wrapper scripts that set
    wrapper_environment. Only the dependency dylibs the formula's binaries actually load are
    copied, flat, into install_prefix/lib. Every Homebrew reference in Mach-O load commands
    (@@HOMEBREW_PREFIX@@, @@HOMEBREW_CELLAR@@, /opt/homebrew, @rpath, @loader_path) is
    rewritten to install_prefix/lib, Homebrew rpaths are removed, and each changed file is
    re-signed ad hoc. etc files from etc_from_formulae (the bottles' .bottle/etc) are copied
    to install_prefix/etc with Homebrew placeholders replaced."""

    description = __doc__
    input_variables = {
        "bottle_manifest": {"required": True, "description": "Manifest from HomebrewBottleDownloader."},
        "install_prefix": {"required": True, "description": "Where the bundle is installed, eg /usr/local/graphviz."},
        "pkgroot": {"required": True, "description": "Package root; the bundle goes in pkgroot + install_prefix."},
        "wrapper_environment": {
            "required": False,
            "default": {},
            "description": "Environment variables set by the bin wrappers. {prefix} is replaced with "
            "install_prefix. A value ending in '?' is only set if not already set.",
        },
        "etc_from_formulae": {
            "required": False,
            "default": [],
            "description": "Formulae whose .bottle/etc files are copied to install_prefix/etc.",
        },
    }
    output_variables = {
        "bundled_dylib_count": {"description": "Number of dependency dylibs bundled."},
    }

    def main(self) -> None:
        with open(self.env["bottle_manifest"]) as f:
            manifest = json.load(f)
        prefix = self.env["install_prefix"].rstrip("/")
        dest = os.path.join(self.env["pkgroot"], prefix.lstrip("/"))
        work = os.path.join(self.env["RECIPE_CACHE_DIR"], "bottles_unpacked")
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        if os.path.exists(dest):
            shutil.rmtree(dest)

        # Unpack every bottle; each holds <name>/<version>/...
        kegs = {}
        for bottle in manifest["bottles"]:
            # bsdtar refuses absolute and '..' paths unless -P is given
            run(["/usr/bin/tar", "-xzf", bottle["path"], "-C", work])
            keg = os.path.join(work, bottle["keg_dir"])
            if not os.path.isdir(keg):
                raise ProcessorError(f"Unexpected layout in {bottle['path']}: no {bottle['keg_dir']}")
            kegs[bottle["name"]] = keg
        top = manifest["bottles"][-1]["name"]
        top_keg = kegs[top]

        # Index every dependency library by file name
        library_index = {}
        for name, keg in kegs.items():
            if name == top:
                continue
            for root, _, files in os.walk(os.path.join(keg, "lib")):
                for fn in files:
                    if fn.endswith(".dylib") or ".so" in fn:
                        library_index.setdefault(fn, os.path.join(root, fn))

        # Copy the formula's own keg: executables to libexec, libraries and data as-is
        os.makedirs(os.path.join(dest, "bin"))
        shutil.copytree(os.path.join(top_keg, "bin"), os.path.join(dest, "libexec"), symlinks=True)
        shutil.copytree(os.path.join(top_keg, "lib"), os.path.join(dest, "lib"), symlinks=True,
                        ignore=shutil.ignore_patterns("pkgconfig", "*.la", "*.a", "cmake"))
        if os.path.isdir(os.path.join(top_keg, "share")):
            shutil.copytree(os.path.join(top_keg, "share"), os.path.join(dest, "share"), symlinks=True)

        lib_dir = os.path.join(dest, "lib")
        # The formula's own libraries and plugins, by file name -> path relative to the prefix
        own_libs = {}
        for root, _, files in os.walk(lib_dir):
            for fn in files:
                own_libs.setdefault(fn, os.path.relpath(os.path.join(root, fn), dest))

        def macho_files():
            for sub in ("libexec", "lib"):
                for root, _, files in os.walk(os.path.join(dest, sub)):
                    for fn in files:
                        path = os.path.join(root, fn)
                        if is_macho(path):
                            yield path

        def needs_rewrite(dep):
            return not dep.startswith(SYSTEM_PREFIXES)

        def own_id(path):
            lines = run(["/usr/bin/otool", "-D", path]).splitlines()[1:]
            return lines[0].strip() if lines else None

        def deps_of(path):
            # otool -L also lists a dylib's own install name; that isn't a dependency
            ident = own_id(path)
            lines = run(["/usr/bin/otool", "-L", path]).splitlines()[1:]
            deps = [l.strip().split(" (compatibility")[0] for l in lines if l.strip()]
            return [d for d in deps if d != ident]

        def target_for(dep):
            base = os.path.basename(dep)
            return f"{prefix}/{own_libs[base]}" if base in own_libs else f"{prefix}/lib/{base}"

        # Pull in the dependency dylibs the bundle actually loads, transitively
        bundled = set()
        queue = list(macho_files())
        while queue:
            path = queue.pop()
            for dep in deps_of(path):
                base = os.path.basename(dep)
                if not needs_rewrite(dep) or base in own_libs or base in bundled:
                    continue
                source = library_index.get(base)
                if not source:
                    raise ProcessorError(f"{os.path.basename(path)} needs {dep}, which isn't in any bottle")
                target = os.path.join(lib_dir, base)
                shutil.copy2(os.path.realpath(source), target)
                os.chmod(target, 0o755)
                bundled.add(base)
                queue.append(target)
        self.output(f"Bundled {len(bundled)} dependency libraries")

        # Bottle files are often read-only; make them owner-writable so they can be rewritten
        # here and updated in place after install (eg graphviz's 'dot -c' rewrites config8)
        for root, _, files in os.walk(dest):
            for fn in files:
                path = os.path.join(root, fn)
                if not os.path.islink(path):
                    os.chmod(path, os.stat(path).st_mode | 0o200)

        # Rewrite load commands, ids and rpaths, then re-sign
        for path in macho_files():
            args = []
            for dep in deps_of(path):
                if needs_rewrite(dep):
                    args += ["-change", dep, target_for(dep)]
            if path.endswith(".dylib") or ".so" in os.path.basename(path):
                rel = os.path.relpath(path, dest)
                args += ["-id", f"{prefix}/{rel}"]
            rpaths = []
            load = run(["/usr/bin/otool", "-l", path]).splitlines()
            for i, line in enumerate(load):
                if "cmd LC_RPATH" in line:
                    rp = load[i + 2].strip().split(" (offset")[0].replace("path ", "", 1)
                    rpaths.append(rp)
            for rp in rpaths:
                args += ["-delete_rpath", rp]
            if args:
                run(["/usr/bin/install_name_tool"] + args + [path])
            run(["/usr/bin/codesign", "--force", "--sign", "-", path])

        # Anything still pointing at Homebrew means the bundle wouldn't work without it
        for path in macho_files():
            leftovers = [d for d in deps_of(path) if needs_rewrite(d) and not d.startswith(prefix + "/")]
            if leftovers:
                raise ProcessorError(f"{path} still references {leftovers}")

        # etc files (eg fontconfig's fonts.conf), with Homebrew placeholders replaced
        for name in self.env.get("etc_from_formulae") or []:
            src = os.path.join(kegs[name], ".bottle", "etc")
            if not os.path.isdir(src):
                raise ProcessorError(f"{name} bottle has no .bottle/etc")
            shutil.copytree(src, os.path.join(dest, "etc"), symlinks=True, dirs_exist_ok=True)
        for root, _, files in os.walk(os.path.join(dest, "etc")):
            for fn in files:
                path = os.path.join(root, fn)
                if os.path.islink(path):
                    continue
                with open(path, "rb") as f:
                    data = f.read()
                new = (data.replace(b"@@HOMEBREW_PREFIX@@/var/cache", b"/Library/Caches")
                       .replace(b"@@HOMEBREW_PREFIX@@", prefix.encode())
                       .replace(b"@@HOMEBREW_CELLAR@@", prefix.encode()))
                if new != data:
                    with open(path, "wb") as f:
                        f.write(new)

        # bin wrappers that set the environment and run the real executable
        env_lines = []
        for key, value in (self.env.get("wrapper_environment") or {}).items():
            value = str(value).replace("{prefix}", prefix)
            if value.endswith("?"):
                env_lines.append(f'export {key}="${{{key}:-{value[:-1]}}}"')
            else:
                env_lines.append(f'export {key}="{value}"')
        for fn in sorted(os.listdir(os.path.join(dest, "libexec"))):
            wrapper = os.path.join(dest, "bin", fn)
            with open(wrapper, "w") as f:
                f.write("#!/bin/sh\n" + "\n".join(env_lines) + f'\nexec "{prefix}/libexec/{fn}" "$@"\n')
            os.chmod(wrapper, 0o755)

        shutil.rmtree(work, ignore_errors=True)
        self.env["bundled_dylib_count"] = len(bundled)


if __name__ == "__main__":
    PROCESSOR = HomebrewBottleBundler()
    PROCESSOR.execute_shell()
