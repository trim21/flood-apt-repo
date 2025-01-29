import functools
import re
import json
import os
from pathlib import Path
import subprocess
import tomllib
from typing import Any, TypeVar

import httpx
import dataclasses
from pydantic import TypeAdapter
import pydantic

IS_CI = "CI" in os.environ

headers = {}
if "PAT" in os.environ:
    headers["Authorization"] = "token " + os.environ["PAT"]

client = httpx.Client(headers=headers)


arch_pattern = re.compile(r"Architecture: ([^\n]*)\n")


_T = TypeVar("_T")


@functools.cache
def get_type_adapter(t: type[_T]) -> TypeAdapter[_T]:
    return TypeAdapter(t)


_K = TypeVar("_K")


def parse_obj_as(typ: type[_K], value: Any, *, strict: bool | None = None) -> _K:
    t: TypeAdapter[_K] = get_type_adapter(typ)  # type: ignore[arg-type]
    return t.validate_python(value, strict=strict)


@dataclasses.dataclass(kw_only=True, slots=True, frozen=True)
class Config:
    output_dir: Path
    suite: str
    component: str
    architectures: list[str]
    repositories: list[str]


@dataclasses.dataclass(kw_only=True, slots=True, frozen=True)
class Asset:
    name: str
    browser_download_url: str


@dataclasses.dataclass(kw_only=True, slots=True, frozen=True)
class Release:
    tag_name: str
    assets: list[Asset]


config = parse_obj_as(
    Config, tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))
)

pool_root = config.output_dir.joinpath("pool", config.component)
release_dir = config.output_dir.joinpath("dists", config.suite)

pool_root.mkdir(exist_ok=True, parents=True)
release_dir.mkdir(exist_ok=True, parents=True)


cache_file_path = config.output_dir.joinpath("hash-cache.json")


@dataclasses.dataclass(kw_only=True, slots=True, frozen=True)
class Cache:
    tag: str
    filename: str
    arch: str
    package: str


package_cache: list[Cache] = []
try:
    package_cache = parse_obj_as(list[Cache], json.loads(cache_file_path.read_bytes()))
except (json.JSONDecodeError, pydantic.ValidationError):
    cache_file_path.unlink(missing_ok=True)
except FileNotFoundError:
    pass

try:
    for repo in config.repositories:
        for tag in parse_obj_as(
            list[Release],
            (
                client.get(
                    f"https://api.github.com/repos/{repo}/releases",
                    headers=headers,
                    params={"per_page": 100},
                )
                .raise_for_status()
                .json()
            ),
        ):
            for asset in tag.assets:
                if not asset.name.endswith(".deb"):
                    continue
                if any(
                    (cache.tag == tag.tag_name and cache.filename == asset.name)
                    for cache in package_cache
                ):
                    continue
                local_dir = pool_root.joinpath(repo, tag.tag_name)
                local_dir.mkdir(exist_ok=True, parents=True)
                local_name = local_dir.joinpath(asset.name)
                deb = client.get(asset.browser_download_url, follow_redirects=True)
                local_name.write_bytes(deb.content)
                if IS_CI:
                    package = subprocess.check_output(
                        ["dpkg-scanpackages", "--multiversion", "."],
                        cwd=config.output_dir,
                    )
                    pkg = package.decode()
                    m = arch_pattern.search(pkg)
                    if not m:
                        raise ValueError(
                            "can not find arch in package file {!r}".format(pkg)
                        )
                    arch = m.group(1)
                    package_cache.append(
                        Cache(
                            tag=tag.tag_name,
                            filename=asset.name,
                            arch=arch,
                            package=pkg,
                        )
                    )

                    local_name.unlink()
finally:
    cache_file_path.write_text(
        json.dumps(
            [dataclasses.asdict(c) for c in package_cache], ensure_ascii=False, indent=2
        )
    )

# packages_file = open(os.path.join(release_dir, "Packages"), "w")
# for repo in config.repositories:
#     print(f"{repo}:", file=sys.stderr)
#     try:
#         latest = requests.get(
#             f"https://api.github.com/repos/{repo}/releases/latest", headers=headers
#         ).json()
#         tag_name = latest["tag_name"]
#         for asset in latest["assets"]:
#             if not asset["name"].endswith(".deb"):
#                 print(f"  {asset['name']}: skipped", file=sys.stderr)
#                 continue
#             print(f"  {asset['name']}: download", file=sys.stderr)
#             local_dir = os.path.join(pool_root, repo, tag_name)
#             local_name = os.path.join(local_dir, asset["name"])

#             os.makedirs(local_dir, exist_ok=True)
#             if os.path.isfile(local_name):
#                 print(f"    {local_name} already exists", file=sys.stderr)
#                 continue

#             resp = requests.get(asset["browser_download_url"], stream=True)
#             if not resp.ok:
#                 continue
#             with open(local_name, "wb") as f:
#                 for chunk in resp.iter_content(chunk_size=1024 * 1024):
#                     f.write(chunk)
#         # In CI with limited disk space, process repositories one by one
#         if "CI" in os.environ:
#             subprocess.run(
#                 ["dpkg-scanpackages", "--multiversion", "."],
#                 stdin=subprocess.DEVNULL,
#                 stdout=packages_file,
#                 cwd=output_dir,
#                 check=True,
#             )
#             with contextlib.suppress(FileNotFoundError):
#                 shutil.rmtree(pool_root)
#     except Exception:
#         traceback.print_exc()

# if "CI" not in os.environ:
#     subprocess.run(
#         ["dpkg-scanpackages", "--multiversion", "."],
#         stdin=subprocess.DEVNULL,
#         stdout=packages_file,
#         cwd=output_dir,
#         check=True,
#     )
# packages_file.close()

# # Split the "Packages" file by architecture
# try:
#     packages = {}
#     for arch in architectures:
#         arch_dir = os.path.join(release_dir, component, f"binary-{arch}")
#         os.makedirs(arch_dir, exist_ok=True)
#         packages[arch] = open(os.path.join(arch_dir, "Packages"), "w")

#     buf = []
#     arch = None
#     with open(packages_file.name, "r") as f:
#         for line in f:
#             if not line.strip():
#                 if arch in architectures:
#                     print("".join(buf), file=packages[arch])
#                 buf = []
#                 arch = None
#                 continue
#             buf.append(line)
#             if line.startswith("Architecture:"):
#                 arch = line.split()[1].strip()
#     os.remove(packages_file.name)
# finally:
#     for f in packages.values():
#         f.close()


# # Generate the "Release" file
with release_dir.joinpath("Release").open("wb") as f:
    subprocess.run(
        ["apt-ftparchive", "-c", f"{config.suite}.conf", "release", release_dir],
        stdin=subprocess.DEVNULL,
        stdout=f,
        check=True,
    )
