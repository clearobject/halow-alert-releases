#!/usr/bin/env python3
"""Check the built Python package (sdist and wheel) of integrations/python.

    python3 .github/scripts/check_python_dist.py DIST_DIR [--version X.Y.Z]

DIST_DIR holds exactly one wheel and one sdist, as `python -m build` writes them. The
version defaults to __version__ in integrations/python/halow_client.py; the release
workflow passes the tag's version. Checks:
  - the file names: clearobject_halow_client-<version>-py3-none-any.whl and .tar.gz;
  - the wheel holds halow_client.py (byte-identical to the source) and its .dist-info only,
    with the LICENSE (identical to integrations/LICENSE) and the halow-client command;
  - METADATA: name, version, Requires-Python, MIT license, author ClearObject without an
    email, project URLs of this repository only, no dependencies, a Markdown description;
  - the sdist holds the module, pyproject.toml, README.md and LICENSE, and no tests,
    examples or caches;
  - integrations/python/LICENSE is a copy of integrations/LICENSE.
Exit code 1 lists every problem.
"""
import argparse
import email.parser
import os
import re
import sys
import tarfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG_DIR = os.path.join(ROOT, "integrations", "python")
NAME, DIST = "clearobject-halow-client", "clearobject_halow_client"
REPO_URL = "https://github.com/clearobject/halow-alert-releases"
SDIST_REQUIRED = {"PKG-INFO", "pyproject.toml", "halow_client.py", "README.md", "LICENSE"}
SDIST_ALLOWED = SDIST_REQUIRED | {"setup.cfg", "MANIFEST.in"}
EGG_INFO_ALLOWED = {"PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt", "top_level.txt"}

problems = []


def problem(msg):
    problems.append(msg)
    print(f"::error::{msg}")


def read(path):
    with open(path, "rb") as f:
        return f.read()


def source_version():
    with open(os.path.join(PKG_DIR, "halow_client.py"), encoding="utf-8") as f:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M)
    if not m:
        sys.exit("integrations/python/halow_client.py has no __version__")
    return m.group(1)


def canonical(version):
    """The version as setuptools writes it (PEP 440): 0.2.0-rc.1 becomes 0.2.0rc1."""
    try:
        from packaging.version import Version
    except ImportError:
        return version
    return str(Version(version))


def check_metadata(text, version, where):
    msg = email.parser.Parser().parsestr(text)
    expect = {"Name": NAME, "Version": version, "Requires-Python": ">=3.8", "Author": "ClearObject",
              "Description-Content-Type": "text/markdown"}
    for key, want in expect.items():
        if msg.get(key) != want:
            problem(f"{where}: {key} is {msg.get(key)!r}, want {want!r}")
    if msg.get("License-Expression") != "MIT" and msg.get("License") != "MIT":
        problem(f"{where}: no MIT license (License: {msg.get('License')!r}, "
                f"License-Expression: {msg.get('License-Expression')!r})")
    classifiers = msg.get_all("Classifier") or []
    if "License-Expression" not in msg and "License :: OSI Approved :: MIT License" not in classifiers:
        problem(f"{where}: the MIT license classifier is missing")
    if "LICENSE" not in (msg.get_all("License-File") or []):
        problem(f"{where}: License-File: LICENSE is missing")
    for key in ("Author-email", "Maintainer", "Maintainer-email", "Requires-Dist", "Home-page"):
        if key in msg:
            problem(f"{where}: unexpected {key}: {msg.get_all(key)}")
    urls = msg.get_all("Project-URL") or []
    if not urls:
        problem(f"{where}: no Project-URL")
    for u in urls:
        if not u.split(",", 1)[-1].strip().startswith(REPO_URL):
            problem(f"{where}: Project-URL outside {REPO_URL}: {u}")
    if not msg.get_payload().strip():
        problem(f"{where}: the long description (README.md) is empty")
    return msg


def check_wheel(path, version):
    info = f"{DIST}-{version}.dist-info/"
    with zipfile.ZipFile(path) as z:
        names = sorted(z.namelist())
        print(f"wheel {os.path.basename(path)}:")
        for n in names:
            print(f"  {n}")
        license_names = [n for n in (info + "LICENSE", info + "licenses/LICENSE") if n in names]
        allowed = {"halow_client.py"} | {info + n for n in ("METADATA", "WHEEL", "RECORD", "entry_points.txt",
                                                             "top_level.txt", "LICENSE", "licenses/LICENSE")}
        for n in names:
            if n not in allowed:
                problem(f"wheel: unexpected file {n} (the wheel holds halow_client.py and its metadata only)")
        if "halow_client.py" not in names:
            problem("wheel: halow_client.py is missing")
        elif z.read("halow_client.py") != read(os.path.join(PKG_DIR, "halow_client.py")):
            problem("wheel: halow_client.py differs from integrations/python/halow_client.py")
        if not license_names:
            problem(f"wheel: no {info}LICENSE")
        elif z.read(license_names[0]) != read(os.path.join(ROOT, "integrations", "LICENSE")):
            problem("wheel: the LICENSE differs from integrations/LICENSE")
        wheel = z.read(info + "WHEEL").decode("utf-8") if info + "WHEEL" in names else ""
        for line in ("Root-Is-Purelib: true", "Tag: py3-none-any"):
            if line not in wheel.splitlines():
                problem(f"wheel: WHEEL lacks '{line}'")
        eps = z.read(info + "entry_points.txt").decode("utf-8") if info + "entry_points.txt" in names else ""
        if not re.search(r"^\[console_scripts\]\s*\nhalow-client\s*=\s*halow_client:main\s*$", eps, re.M):
            problem(f"wheel: entry_points.txt lacks 'halow-client = halow_client:main': {eps!r}")
        top = z.read(info + "top_level.txt").decode("utf-8").split() if info + "top_level.txt" in names else []
        if top != ["halow_client"]:
            problem(f"wheel: top_level.txt is {top}, want ['halow_client']")
        if info + "METADATA" not in names:
            problem("wheel: METADATA is missing")
            return
        msg = check_metadata(z.read(info + "METADATA").decode("utf-8"), version, "wheel METADATA")
    print("METADATA:")
    for key in ("Metadata-Version", "Name", "Version", "Summary", "Author", "License", "License-Expression",
                "License-File", "Requires-Python", "Description-Content-Type"):
        for v in msg.get_all(key) or []:
            print(f"  {key}: {v}")
    for v in msg.get_all("Project-URL") or []:
        print(f"  Project-URL: {v}")
    print(f"  Classifiers: {len(msg.get_all('Classifier') or [])}")


def check_sdist(path, version):
    top = f"{DIST}-{version}/"
    with tarfile.open(path) as t:
        members = t.getmembers()
        print(f"sdist {os.path.basename(path)}:")
        files = set()
        for m in members:
            print(f"  {m.name}")
            if not m.name.startswith(top) and m.name != top.rstrip("/"):
                problem(f"sdist: {m.name} is outside {top}")
                continue
            if m.issym() or m.islnk():
                problem(f"sdist: {m.name} is a link")
            if m.isfile():
                files.add(m.name[len(top):])
        for f in sorted(files):
            head, _, rest = f.partition("/")
            if f in SDIST_ALLOWED or (head == f"{DIST}.egg-info" and rest in EGG_INFO_ALLOWED):
                continue
            problem(f"sdist: unexpected file {f}")
        for f in sorted(SDIST_REQUIRED - files):
            problem(f"sdist: {f} is missing")
        if "halow_client.py" in files:
            if t.extractfile(top + "halow_client.py").read() != read(os.path.join(PKG_DIR, "halow_client.py")):
                problem("sdist: halow_client.py differs from integrations/python/halow_client.py")
        if "PKG-INFO" in files:
            check_metadata(t.extractfile(top + "PKG-INFO").read().decode("utf-8"), version, "sdist PKG-INFO")


def main(argv):
    p = argparse.ArgumentParser(description="Check the built Python package of integrations/python.")
    p.add_argument("dist", help="folder with the wheel and the sdist")
    p.add_argument("--version", default="", help="expected version (default: __version__ in halow_client.py)")
    a = p.parse_args(argv)
    src = source_version()
    want = a.version or src
    if want != src:
        problem(f"halow_client.__version__ is {src}, but {want} is expected")
    version = canonical(want)
    print(f"expected version: {want} (as built: {version})")

    if read(os.path.join(PKG_DIR, "LICENSE")) != read(os.path.join(ROOT, "integrations", "LICENSE")):
        problem("integrations/python/LICENSE must be a copy of integrations/LICENSE")

    files = sorted(os.listdir(a.dist))
    wheels = [f for f in files if f.endswith(".whl")]
    sdists = [f for f in files if f.endswith(".tar.gz")]
    others = [f for f in files if f not in wheels + sdists]
    if others:
        problem(f"{a.dist}: unexpected files {others}")
    want_wheel, want_sdist = f"{DIST}-{version}-py3-none-any.whl", f"{DIST}-{version}.tar.gz"
    if wheels != [want_wheel]:
        problem(f"{a.dist}: wheels {wheels}, want [{want_wheel}]")
    if sdists != [want_sdist]:
        problem(f"{a.dist}: sdists {sdists}, want [{want_sdist}]")
    if want_wheel in wheels:
        check_wheel(os.path.join(a.dist, want_wheel), version)
    if want_sdist in sdists:
        check_sdist(os.path.join(a.dist, want_sdist), version)

    if problems:
        print(f"check_python_dist: {len(problems)} problem(s)")
        return 1
    print(f"check_python_dist: OK ({want_wheel}, {want_sdist})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
