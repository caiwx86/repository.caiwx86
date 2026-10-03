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

_GITHUB_PREFIXES = (
    "https://github.com/",
    "http://github.com/",
    "git@github.com:",
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def normalize_repo(value: str) -> str:
    """接受 GitHub URL、SSH URL 或 owner/repo 格式。"""
    repo = value.strip().removesuffix(".git").rstrip("/")
    for prefix in _GITHUB_PREFIXES:
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
            break
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
        raise ValueError(f"无效的 GitHub 仓库地址：{value}")
    return repo


def parse_source(value: str) -> tuple[str, str | None]:
    """解析 sources.txt 中的一行。

    支持格式：
        owner/repo                    -> 最新 Release（无 Release 时回退默认分支）
        owner/repo@ref                -> 指定 tag / branch / commit 源码打包
        https://github.com/owner/repo[@ref]
        git@github.com:owner/repo[@ref]
    """
    raw = value.strip()
    if not raw:
        raise ValueError("空的 source 条目")

    cleaned = raw.removesuffix(".git").rstrip("/")
    for prefix in _GITHUB_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break

    ref: str | None = None
    if "@" in cleaned:
        repo_part, _, ref_part = cleaned.rpartition("@")
        if not repo_part:
            raise ValueError(f"无效的 GitHub 仓库地址：{value}")
        cleaned = repo_part
        ref = ref_part.strip() or None

    return normalize_repo(cleaned), ref


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
        allow_redirects=True,
    ) as response:
        response.raise_for_status()
        if stream:
            with destination.open("wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        else:
            destination.write_bytes(response.content)


def download_source_archive(repo: str, ref: str, destination: Path) -> None:
    """下载指定 ref 的源码压缩包（支持 tag / branch / commit）。"""
    url = f"{GITHUB_API}/repos/{repo}/zipball/{ref}"
    download_file(url, destination, stream=True)


def get_default_branch_source(repo: str, destination: Path) -> None:
    """下载仓库默认分支的源码 zipball（不带 ref，GitHub 自动使用默认分支）。"""
    url = f"{GITHUB_API}/repos/{repo}/zipball"
    download_file(url, destination, stream=True)


def get_platform(filename: str) -> str:
    filename = filename.lower()
    return next(
        (platform for platform in STANDARD_PLATFORMS if platform in filename),
        "all",
    )


def _find_addon_xml(names: list[str]) -> str:
    """在 ZIP 条目列表中定位层级最浅的 addon.xml。"""
    candidates = [
        name for name in names if name.rstrip("/").endswith("addon.xml")
    ]
    if not candidates:
        raise ValueError("压缩包内找不到 addon.xml")
    return min(candidates, key=lambda name: (name.count("/"), len(name)))


def _dir_prefix(path: str) -> str:
    """返回 path 所在目录前缀（含结尾斜杠），无目录时返回空串。"""
    return path.rsplit("/", 1)[0] + "/" if "/" in path else ""


def get_addon_metadata(zip_path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(zip_path) as archive:
        addon_xml_path = _find_addon_xml(archive.namelist())
        root = ET.fromstring(archive.read(addon_xml_path))
    addon_id = root.attrib.get("id")
    version = root.attrib.get("version")
    if not addon_id or not version:
        raise ValueError("addon.xml 缺少 id 或 version")
    if not re.fullmatch(r"[a-z0-9._-]+", addon_id):
        raise ValueError(f"不合法的插件 ID：{addon_id}")
    return addon_id, version


def repackage_as_addon(source_zip: Path, target_zip: Path) -> tuple[str, str]:
    """把 GitHub 源码压缩包重新打包成 Kodi 插件包。

    目标结构：zip 根目录下只有一个以 addon_id 命名的文件夹，
    该文件夹内包含 addon.xml 及全部源码/资源。

    返回 ``(addon_id, version)``。
    """
    with zipfile.ZipFile(source_zip) as src:
        addon_xml_path = _find_addon_xml(src.namelist())
        source_root = _dir_prefix(addon_xml_path)  # 例如 "repo-sha/subdir/"

        root_elem = ET.fromstring(src.read(addon_xml_path))
        addon_id = root_elem.attrib.get("id")
        version = root_elem.attrib.get("version")
        if not addon_id or not version:
            raise ValueError("addon.xml 缺少 id 或 version")
        if not re.fullmatch(r"[a-z0-9._-]+", addon_id):
            raise ValueError(f"不合法的插件 ID：{addon_id}")

        target_prefix = f"{addon_id}/"

        with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                name = info.filename
                if not name.startswith(source_root):
                    continue
                rel = name[len(source_root):]
                if not rel or rel.endswith("/"):
                    continue
                dst.writestr(target_prefix + rel, src.read(name))

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

    兼容两种 zip 布局：
      - addon.xml 位于 zip 根目录
      - addon.xml 位于 ``{addon_id}/`` 子目录内
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        try:
            addon_xml_path = _find_addon_xml(names)
        except ValueError:
            print("    ZIP 中未找到 addon.xml，无法提取资源。")
            return

        # addon.xml 所在的目录前缀，例如 "addon.id/"
        prefix = _dir_prefix(addon_xml_path)

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

            archive_path = prefix + clean_path
            if archive_path not in names:
                print(f"    资源文件未在 ZIP 中找到：{clean_path}")
                continue

            target_file = target_dir / clean_path
            if target_file.exists():
                print(f"    资源已存在，跳过：{clean_path}")
                continue

            target_file.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(archive_path) as src, target_file.open("wb") as dst:
                shutil.copyfileobj(src, dst)

            print(f"    已提取资源：{clean_path}")
            extracted_count += 1

        if extracted_count == 0:
            print("    未提取任何新资源文件。")
        else:
            print(f"    成功提取 {extracted_count} 个资源文件。")


# ---------------------------------------------------------------------------
# 更新流程
# ---------------------------------------------------------------------------

def _finalize_package(
    addon_zip: Path,
    addon_id: str,
    version: str,
    platform: str,
) -> int:
    """把已生成的插件 zip 移动到仓库目录并提取资源。返回 1 表示更新成功，0 表示跳过。"""
    addon_dir = Path(addon_id)
    package_name = (
        f"{addon_id}-{version}.zip"
        if platform == "all"
        else f"{addon_id}-{version}-{platform}.zip"
    )
    target_path = addon_dir / package_name

    if target_path.exists():
        print(f"  目标文件已存在，跳过：{target_path}")
        return 0

    addon_dir.mkdir(parents=True, exist_ok=True)
    remove_old_packages(addon_dir, addon_id, platform)
    shutil.move(str(addon_zip), target_path)
    print(f"  更新完成：{target_path}")

    extract_asset_files_from_zip(target_path, addon_dir)
    return 1


def _update_from_release(repo: str) -> int:
    print(f"\n检查：{repo}")

    # ---- 尝试获取最新 Release，404 时回退到默认分支源码 ----
    try:
        release = get_latest_release(repo)
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 404:
            print("  仓库没有 Release，自动回退到默认分支源码打包。")
            return _update_from_default_branch(repo)
        raise

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

                # 判断是否为 GitHub 源码包（zipball）：需要重打包成 {addon_id}/ 结构
                is_source_zip = "source" in filename.lower() or "zipball" in filename.lower()

                if is_source_zip:
                    addon_zip = temp_path / "repacked.zip"
                    addon_id, version = repackage_as_addon(local_zip, addon_zip)
                    updated += _finalize_package(addon_zip, addon_id, version, "all")
                else:
                    # Release 附件，保持原样
                    updated += _finalize_package(local_zip, addon_id, version, platform)

            except Exception as error:
                print(f"  跳过 {filename}：{error}")

    return updated


def _update_from_default_branch(repo: str) -> int:
    """仓库无 Release 时，下载默认分支源码并重打包。"""
    repo_name = repo.split("/")[-1]
    source_filename = f"{repo_name}-source.zip"

    with tempfile.TemporaryDirectory(prefix="kodi-addon-") as temp_dir:
        temp_path = Path(temp_dir)
        source_zip = temp_path / source_filename
        addon_zip = temp_path / "addon.zip"

        try:
            print(f"  下载默认分支源码包：{repo_name}")
            get_default_branch_source(repo, source_zip)

            addon_id, version = repackage_as_addon(source_zip, addon_zip)
            return _finalize_package(addon_zip, addon_id, version, "all")

        except Exception as error:
            print(f"  跳过 {repo}（默认分支）：{error}")
            return 0


def _update_from_ref(repo: str, ref: str) -> int:
    """下载指定 ref（tag / branch / commit）的源码并重打包。"""
    print(f"\n检查：{repo}（指定版本：{ref}）")

    repo_name = repo.split("/")[-1]
    safe_ref = re.sub(r"[^A-Za-z0-9._-]+", "-", ref).strip("-") or "ref"
    source_filename = f"{repo_name}-{safe_ref}-source.zip"

    with tempfile.TemporaryDirectory(prefix="kodi-addon-") as temp_dir:
        temp_path = Path(temp_dir)
        source_zip = temp_path / source_filename
        addon_zip = temp_path / "addon.zip"

        try:
            print(f"  下载源码包：{ref}")
            download_source_archive(repo, ref, source_zip)

            addon_id, version = repackage_as_addon(source_zip, addon_zip)
            return _finalize_package(addon_zip, addon_id, version, "all")

        except Exception as error:
            print(f"  跳过 {repo}@{ref}：{error}")
            return 0


def update_repo(source: str) -> int:
    repo, ref = parse_source(source)
    if ref:
        return _update_from_ref(repo, ref)
    return _update_from_release(repo)


# ---------------------------------------------------------------------------
# addons.xml 生成
# ---------------------------------------------------------------------------

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
                    try:
                        addon_xml_path = _find_addon_xml(zf.namelist())
                    except ValueError:
                        print(f"警告：{zip_path} 中未找到 addon.xml，跳过")
                        continue

                    xml_content = zf.read(addon_xml_path)
                    addon_root = ET.fromstring(xml_content)

                    new_elem = ET.SubElement(root_elem, "addon")
                    for key, value in addon_root.attrib.items():
                        new_elem.set(key, value)
                    for child in addon_root:
                        new_elem.append(child)

                    print(
                        f"已添加索引条目："
                        f"{new_elem.attrib.get('id')} v{new_elem.attrib.get('version')}"
                    )

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
