import contextlib
import functools
import re
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from typing import Any, TypeVar

import semver
import httpx
import dataclasses
from pydantic import TypeAdapter
import pydantic

IS_CI = "CI" in os.environ

headers = {}
if "PAT" in os.environ:
    headers["Authorization"] = "token " + os.environ["PAT"]

client = httpx.Client(headers=headers)


arch_pattern = re.compile("Architecture: (\\S+)\n")


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
    # architectures: list[str]
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


root = Path(__file__).parent
public = root.joinpath("public")


def copy_public_files():
    for dir, _, files in os.walk(public):
        for file in files:
            out_file = Path(dir, file)
            print(Path(dir, file))
            config.output_dir.joinpath(out_file.relative_to(public)).write_bytes(
                out_file.read_bytes()
            )


@dataclasses.dataclass(kw_only=True, slots=True, frozen=True)
class Cache:
    tag: str
    filename: str
    arch: str
    package: str


def main():
    pool_root = config.output_dir.joinpath("pool", config.component)
    release_dir = config.output_dir.joinpath("dists", config.suite)

    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(release_dir)

    release_dir.mkdir(parents=True, exist_ok=True)
    pool_root.mkdir(exist_ok=True, parents=True)

    package_cache: list[Cache] = []
    cache_file_path = config.output_dir.joinpath("package-cache.json")
    try:
        package_cache = parse_obj_as(
            list[Cache], json.loads(cache_file_path.read_bytes())
        )
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
                print("processing", tag.tag_name, file=sys.stderr, flush=True)
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
                    print(
                        "processing",
                        tag.tag_name,
                        asset.name,
                        file=sys.stderr,
                        flush=True,
                    )
                    deb = client.get(
                        asset.browser_download_url, follow_redirects=True, timeout=30
                    )
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
        # remove leading prefix `v`
        package_cache.sort(
            key=lambda c: (semver.Version.parse(c.tag[1:]), c.arch), reverse=True
        )
        new_packages = json.dumps(
            [dataclasses.asdict(c) for c in package_cache],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        changed = True
        if cache_file_path.exists():
            changed = new_packages != cache_file_path.read_text(encoding="utf-8")
        if changed:
            print("package changes")
            cache_file_path.write_text(new_packages)

    for release in package_cache:
        top_dir = release_dir.joinpath(
            config.component, "binary-{}".format(release.arch)
        )
        top_dir.mkdir(exist_ok=True, parents=True)
        with top_dir.joinpath("Packages").open("a+") as f:
            f.write(release.package)

    # Generate the "Release" file
    if IS_CI and changed:
        with release_dir.joinpath("Release").open("wb") as f:
            subprocess.run(
                [
                    "apt-ftparchive",
                    "-c",
                    f"{config.suite}.conf",
                    "release",
                    release_dir,
                ],
                stdin=subprocess.DEVNULL,
                stdout=f,
                check=True,
            )


copy_public_files()
main()
