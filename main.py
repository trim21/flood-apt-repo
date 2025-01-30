import contextlib
import dataclasses
import functools
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from operator import itemgetter
from pathlib import Path
from typing import Any, TypeVar

import httpx
import tomllib
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
    published_at: datetime


@dataclasses.dataclass(kw_only=True, slots=True, frozen=True)
class PackageCache:
    repo: str
    tag: str
    filename: str
    published_at: datetime
    arch: str
    package: str


root = Path(__file__).parent
public = root.joinpath("public")

config = parse_obj_as(
    Config, tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))
)

pool_root = config.output_dir.joinpath("pool", config.component)
release_dir = config.output_dir.joinpath("dists", config.suite)


def copy_public_files():
    for dir, _, files in os.walk(public):
        for file in files:
            src_file = Path(dir, file)
            dist_file = config.output_dir.joinpath(src_file.relative_to(public))
            print(
                "copy",
                src_file.relative_to(root).as_posix(),
                "to",
                dist_file.as_posix(),
                flush=True,
            )
            dist_file.write_bytes(src_file.read_bytes())


def handle_repo(repo: str):
    package_cache: list[PackageCache] = []
    cache_file_path = config.output_dir.joinpath(
        "package-cache-{}.json".format(repo.replace("/", "-"))
    )
    try:
        package_cache = parse_obj_as(
            list[PackageCache], json.loads(cache_file_path.read_bytes())
        )
    except (json.JSONDecodeError, pydantic.ValidationError):
        cache_file_path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass

    find = False

    packages = []

    try:
        for tag in parse_obj_as(
            list[Release],
            sorted(
                client.get(
                    f"https://api.github.com/repos/{repo}/releases",
                    headers=headers,
                    params={"per_page": 100},
                )
                .raise_for_status()
                .json(),
                key=itemgetter("published_at"),
                reverse=True,
            ),
        ):
            if find:
                break
            print("processing", repo, tag.tag_name, file=sys.stderr, flush=True)
            for asset in tag.assets:
                if not asset.name.endswith(".deb"):
                    continue
                if any(
                    (
                        cache.repo == repo
                        and cache.tag == tag.tag_name
                        and cache.filename == asset.name
                    )
                    for cache in package_cache
                ):
                    continue
                find = True
                local_dir = pool_root.joinpath(repo, tag.tag_name)
                local_dir.mkdir(exist_ok=True, parents=True)
                local_name = local_dir.joinpath(asset.name)
                print(
                    "processing",
                    repo,
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
                    c = PackageCache(
                        repo=repo,
                        tag=tag.tag_name,
                        filename=asset.name,
                        arch=arch,
                        package=pkg,
                        published_at=tag.published_at,
                    )
                    package_cache.append(c)
                    packages.append(c)

                    local_name.unlink()
    finally:
        package_cache.sort(key=lambda c: (c.published_at, c.filename), reverse=True)
        cache_file_path.write_text(
            json.dumps(
                [dataclasses.asdict(c) for c in package_cache],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=encode_json,
            )
        )

    for release in packages:
        top_dir = release_dir.joinpath(
            config.component, "binary-{}".format(release.arch)
        )
        top_dir.mkdir(exist_ok=True, parents=True)
        with top_dir.joinpath("Packages").open("a+") as f:
            f.write(release.package)


def main():
    config.output_dir.mkdir(exist_ok=True, parents=True)

    copy_public_files()

    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(release_dir.joinpath(config.component))

    release_dir.mkdir(parents=True, exist_ok=True)
    pool_root.mkdir(exist_ok=True, parents=True)

    for repo in sorted(config.repositories):
        handle_repo(repo)

    # Generate the "Release" file
    if IS_CI:
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


def encode_json(val: Any):
    if isinstance(val, datetime):
        return val.astimezone(UTC).isoformat()
    raise TypeError(f"Cannot serialize object of {type(val)}")


main()
