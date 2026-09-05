from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.dom import minidom

import requests

GITHUB_API = "https://api.github.com"
TIMEOUT = 60

STANDARD_PLATFORMS = (
    "android-aarch64",
    "android-armv7",
    "linux-aarch64",
    "linux-armv7",
    "linux-i686",
    "linux-x86_64",
    "windows-i686",
    "windows-x86_64",
    "osx-x86_64",
    "osx-arm64",
    "ios-arm64",
)


def normalize_repo(value: str) -> str:
    """接受 GitHub URL、SSH URL 或 owner/repo 格式。"""
    repo = value.strip().removesuffix(".git").rstrip("/")
    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "git@github.com:",
    ):
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
            break
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
        raise ValueError(f"无效的 GitHub 仓库地址：{value}")
    return repo


def github_headers() -> dict[str, str]:
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("未检测到 GH_TOKEN 或 GITHUB_TOKEN，拒绝使用匿名 GitHub API。")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def raise_for_api_error(response: requests.Response, repo: str) -> None:
    if response.status_code in (403, 429):
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        if remaining == "0" and reset:
            reset_time = datetime.fromtimestamp(
                int(reset), tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
            raise RuntimeError(
                f"{repo}：GitHub API 已限流，将于 {reset_time} 恢复。"
            )
    response.raise_for_status()


def get_latest_release(repo: str) -> dict:
    response = requests.get(
        f"{GITHUB_API}/repos/{repo}/releases/latest",
        headers=github_headers(),
        timeout=TIMEOUT,
    )
    raise_for_api_error(response, repo)
    return response.json()


def download_file(url: str, destination: Path, *, stream: bool = False) -> None:
    with requests.get(
        url,
        headers=github_headers(),
        stream=stream,
        timeout=TIMEOUT,
    ) as response:
        response.raise_for_status()
        if stream:
            with destination.open("wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        else:
            destination.write_bytes(response.content)


def get_platform(filename: str) -> str:
    filename = filename.lower()
    return next(
        (platform for platform in STANDARD_PLATFORMS if platform in filename),
        "all",
    )


def get_addon_metadata(zip_path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(zip_path) as archive:
        candidates = [
            name
            for name in archive.namelist()
            if name.rstrip("/").endswith("addon.xml")
        ]
        if not candidates:
            raise ValueError("压缩包内找不到 addon.xml")
        addon_xml_path = min(candidates, key=lambda name: name.count("/"))
        root = ET.fromstring(archive.read(addon_xml_path))
    addon_id = root.attrib.get("id")
    version = root.attrib.get("version")
    if not addon_id or not version:
        raise ValueError("addon.xml 缺少 id 或 version")
    if not re.fullmatch(r"[a-z0-9._-]+", addon_id):
        raise ValueError(f"不合法的插件 ID：{addon_id}")
    return addon_id, version


def remove_old_packages(addon_dir: Path, addon_id: str, platform: str) -> None:
    for package in addon_dir.glob(f"{addon_id}-*.zip"):
        filename = package.name
        if platform == "all":
            is_platform_package = any(
                filename.endswith(f"-{item}.zip")
                for item in STANDARD_PLATFORMS
            )
            if not is_platform_package:
                package.unlink()
        elif filename.endswith(f"-{platform}.zip"):
            package.unlink()


def extract_asset_files_from_zip(zip_path: Path, target_dir: Path) -> None:
    """
    从 ZIP 包中读取 addon.xml，提取 <assets> 内的所有资源文件（icon、fanart、screenshot 等），
    并解压到 target_dir 中，保持相对路径结构。
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        addon_xml_path = None
        for name in names:
            if name.rstrip("/").endswith("addon.xml"):
                if addon_xml_path is None or name.count("/") < addon_xml_path.count("/"):
                    addon_xml_path = name
        if not addon_xml_path:
            print("    ZIP 中未找到 addon.xml，无法提取资源。")
            return

        xml_content = zf.read(addon_xml_path)
        root = ET.fromstring(xml_content)

        metadata_extension = root.find("./extension[@point='xbmc.addon.metadata']")
        if metadata_extension is None:
            print("    未找到 <extension point='xbmc.addon.metadata'>，无法提取资源。")
            return

        assets_elem = metadata_extension.find("assets")
        if assets_elem is None:
            print("    未找到 <assets> 节点，无资源可提取。")
            return

        resource_paths = []
        for child in assets_elem:
            if child.text:
                path = child.text.strip()
                if path:
                    resource_paths.append(path)

        if not resource_paths:
            print("    <assets> 中未定义任何资源文件。")
            return

        resource_paths = list(dict.fromkeys(resource_paths))

        extracted_count = 0
        for rel_path in resource_paths:
            clean_path = rel_path.lstrip("/.")
            if not clean_path:
                continue

            if clean_path not in names:
                print(f"    资源文件未在 ZIP 中找到：{clean_path}")
                continue

            target_file = target_dir / clean_path
            if target_file.exists():
                print(f"    资源已存在，跳过：{clean_path}")
                continue

            target_file.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(clean_path) as src, target_file.open("wb") as dst:
                shutil.copyfileobj(src, dst)

            print(f"    已提取资源：{clean_path}")
            extracted_count += 1

        if extracted_count == 0:
            print("    未提取任何新资源文件。")
        else:
            print(f"    成功提取 {extracted_count} 个资源文件。")


def update_repo(source: str) -> int:
    repo = normalize_repo(source)
    print(f"\n检查：{repo}")

    release = get_latest_release(repo)

    # 1. 优先使用附件
    assets = [
        asset
        for asset in release.get("assets", [])
        if asset["name"].lower().endswith(".zip")
    ]

    # 2. 若没有附件，回退到自动生成的 Source code zip
    if not assets:
        zipball_url = release.get("zipball_url")
        if zipball_url:
            repo_name = repo.split("/")[-1]
            filename = f"{repo_name}-source.zip"
            assets = [{"name": filename, "browser_download_url": zipball_url}]
            print("  未找到附件，将使用自动生成的 Source code zip。")
        else:
            print("  最新 Release 没有 ZIP 文件且无源码包，跳过。")
            return 0

    updated = 0

    with tempfile.TemporaryDirectory(prefix="kodi-addon-") as temp_dir:
        temp_path = Path(temp_dir)

        for asset in assets:
            filename = asset["name"]
            local_zip = temp_path / filename

            try:
                print(f"  下载：{filename}")
                download_file(asset["browser_download_url"], local_zip, stream=True)

                addon_id, version = get_addon_metadata(local_zip)
                platform = get_platform(filename)

                package_name = (
                    f"{addon_id}-{version}.zip"
                    if platform == "all"
                    else f"{addon_id}-{version}-{platform}.zip"
                )

                addon_dir = Path(addon_id)
                target_path = addon_dir / package_name

                if target_path.exists():
                    print(f"  目标文件已存在，跳过：{target_path}")
                    local_zip.unlink()
                    continue

                addon_dir.mkdir(parents=True, exist_ok=True)
                remove_old_packages(addon_dir, addon_id, platform)
                shutil.move(str(local_zip), target_path)
                print(f"  更新完成：{target_path}")

                extract_asset_files_from_zip(target_path, addon_dir)

                updated += 1

            except Exception as error:
                print(f"  跳过 {filename}：{error}")

    return updated


def prettify_xml(elem: ET.Element) -> str:
    rough_string = ET.tostring(elem, encoding="utf-8")
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")


def generate_addons_xml() -> None:
    root_elem = ET.Element("addons")

    for dir_path in Path(".").iterdir():
        if not dir_path.is_dir() or dir_path.name.startswith("."):
            continue

        zip_files = list(dir_path.glob("*.zip"))
        if not zip_files:
            continue

        for zip_path in zip_files:
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    names = zf.namelist()
                    addon_xml_path = None
                    for name in names:
                        if name.rstrip("/").endswith("addon.xml"):
                            if addon_xml_path is None or name.count("/") < addon_xml_path.count("/"):
                                addon_xml_path = name
                    if not addon_xml_path:
                        print(f"警告：{zip_path} 中未找到 addon.xml，跳过")
                        continue

                    xml_content = zf.read(addon_xml_path)
                    addon_root = ET.fromstring(xml_content)

                    new_elem = ET.SubElement(root_elem, "addon")
                    for key, value in addon_root.attrib.items():
                        new_elem.set(key, value)
                    for child in addon_root:
                        new_elem.append(child)

                    print(f"已添加索引条目：{new_elem.attrib.get('id')} v{new_elem.attrib.get('version')}")

            except Exception as e:
                print(f"处理 {zip_path} 时出错：{e}，跳过")

    pretty_xml = prettify_xml(root_elem)
    with open("addons.xml", "w", encoding="utf-8") as f:
        f.write(pretty_xml)

    with open("addons.xml", "rb") as f:
        md5_hash = hashlib.md5(f.read()).hexdigest()
    with open("addons.xml.md5", "w") as f:
        f.write(md5_hash)

    print("✅ addons.xml 及 addons.xml.md5 已生成（已美化排版）。")


def main() -> None:
    sources_file = Path("sources.txt")
    if not sources_file.is_file():
        raise FileNotFoundError("找不到 sources.txt")

    sources = [
        line.strip()
        for line in sources_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    if not sources:
        raise ValueError("sources.txt 中没有仓库地址")

    total = 0
    for source in sources:
        try:
            total += update_repo(source)
        except Exception as error:
            print(f"\n处理失败：{source}\n原因：{error}")
            raise

    print(f"\n完成，共更新 {total} 个插件包。")
    generate_addons_xml()


if __name__ == "__main__":
    main()