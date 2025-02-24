import contextlib
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from operator import itemgetter
from pathlib import Path
from typing import Any

import httpx
import pydantic
from pydantic import AwareDatetime, TypeAdapter

IS_CI = "CI" in os.environ

headers = {}
if "PAT" in os.environ:
    headers["Authorization"] = "token " + os.environ["PAT"]

client = httpx.Client(headers=headers)


arch_pattern = re.compile("Architecture: (\\S+)\n")


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
    published_at: AwareDatetime


@dataclasses.dataclass(kw_only=True, slots=True, frozen=True)
class PackageCache:
    tag: str
    filename: str
    published_at: datetime
    arch: str
    package: str


root = Path(__file__).parent
public = root.joinpath("public")

config = TypeAdapter(Config).validate_python(
    tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))
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


zero_time = datetime(2020, 1, 1, tzinfo=UTC)


def handle_repo(repo: str) -> datetime:
    latest_update = zero_time
    package_cache: list[PackageCache] = []
    cache_file_path = config.output_dir.joinpath(
        "package-cache-{}.json".format(repo.replace("/", "-"))
    )
    try:
        package_cache = TypeAdapter(list[PackageCache]).validate_python(
            json.loads(cache_file_path.read_bytes())
        )
    except (json.JSONDecodeError, pydantic.ValidationError):
        cache_file_path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass

    find = False

    packages = []

    try:
        for tag in TypeAdapter(list[Release]).validate_python(
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

            if all(not asset.name.endswith(".deb") for asset in tag.assets):
                continue

            latest_update = max(tag.published_at, latest_update)
            find = True

            for asset in tag.assets:
                if not asset.name.endswith(".deb"):
                    continue

                cached = [
                    cache
                    for cache in package_cache
                    if (cache.tag == tag.tag_name and cache.filename == asset.name)
                ]
                if cached:
                    packages.append(cached[0])
                    continue
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

    return latest_update


def main():
    config.output_dir.mkdir(exist_ok=True, parents=True)

    copy_public_files()

    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(release_dir.joinpath(config.component))

    release_dir.mkdir(parents=True, exist_ok=True)
    pool_root.mkdir(exist_ok=True, parents=True)

    latest_update = zero_time

    for repo in sorted(config.repositories):
        latest_update = max(latest_update, handle_repo(repo))

    # Generate the "Release" file
    if IS_CI:
        with release_dir.joinpath("Release").open("wb") as f:
            subprocess.run(
                [
                    "fatetime",
                    str(
                        latest_update.astimezone(UTC).isoformat(
                            sep=" ", timespec="seconds"
                        )
                    ),
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
