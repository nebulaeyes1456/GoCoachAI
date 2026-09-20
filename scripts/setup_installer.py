# -*- coding: utf-8 -*-
"""弈友 v2 安装程序（setup.exe 本体）。

打包方式（在项目根执行）：

  .venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed ^
    --name "弈友安装程序" --paths "." --distpath "." --workpath build --specpath build ^
    --add-data "%CD%\dist\弈友;payload" scripts\setup_installer.py

要点：
  - **内嵌整棵 dist\弈友 目录为 payload/**（build_desktop.py 是 onedir 构建：
    弈友.exe 只有 8 MB，真正的运行库在 _internal\ 里 100+ MB）——只内嵌 exe
    会装出打不开的程序；
  - **add-data 必须给绝对路径**：`--specpath build` 会把相对路径解析到 build\
    下，报 "Unable to find ...弈友.exe"；
  - 前置：先跑 scripts/build_desktop.py 生成 dist\弈友\，并把其中的
    弈友.exe 复制到项目根（桌面快捷方式指向处）。

运行行为：
  - 正常双击：把内置「弈友.exe」安装到 %LOCALAPPDATA%\\Programs\\弈友\\，
    创建桌面与开始菜单快捷方式、写入卸载注册表（控制面板可卸载）、生成卸载.bat，
    完成后询问是否立即启动。全程无需管理员权限。
  - 测试模式（环境变量 YIYOU_TEST_DIR 指向目录）：只安装到该目录并输出结果，
    不弹窗、不写真实快捷方式/注册表。
"""
import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

APP_NAME = "弈友"
EXE_NAME = "弈友.exe"
VERSION = "1.7.7"

TEST_DIR = os.environ.get("YIYOU_TEST_DIR")

_CREATE_NO_WINDOW = 0x08000000


def resource_path(name: str) -> Path:
    base = getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
    return Path(base) / name


def payload_root() -> Optional[Path]:
    """内嵌载荷的根目录。

    - onedir 构建（当前 build_desktop.py）：整棵「弈友」目录内嵌为 payload/，
      必须整体安装——只装 exe 会缺 _internal/ 而无法启动；
    - onefile 构建（旧）：单个 弈友.exe 内嵌在根，直接复制即可。
    """
    d = resource_path("payload")
    if d.is_dir():
        return d
    return None


def install_dir() -> Path:
    if TEST_DIR:
        return Path(TEST_DIR)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / APP_NAME


def ps_exec(script: str) -> int:
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
        capture_output=True,
        creationflags=_CREATE_NO_WINDOW,
    )
    return r.returncode


def make_shortcut(target: Path, lnk: Path, desc: str) -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$ws = New-Object -ComObject WScript.Shell;"
        + f"$sc = $ws.CreateShortcut('{lnk}');"
        + f"$sc.TargetPath = '{target}';"
        + f"$sc.WorkingDirectory = '{target.parent}';"
        + f"$sc.Description = '{desc}';"
        + "$sc.Save()"
    )
    ps_exec(script)


def desktop_dir() -> Path:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, text=True, creationflags=_CREATE_NO_WINDOW,
    )
    p = (r.stdout or "").strip()
    return Path(p) if p else Path.home() / "Desktop"


def start_menu_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def write_uninstaller(inst: Path) -> None:
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        'taskkill /f /im "弈友.exe" >nul 2>&1',
        f'del /f /q "{desktop_dir() / "弈友.lnk"}" >nul 2>&1',
        f'del /f /q "{start_menu_dir() / "弈友.lnk"}" >nul 2>&1',
        r'reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\YiYou" /f >nul 2>&1',
        f'rmdir /s /q "{inst}"',
        "exit",
    ]
    (inst / "卸载.bat").write_text("\r\n".join(lines), encoding="gbk")


def write_uninstall_registry(inst: Path) -> None:
    if TEST_DIR:
        return
    try:
        import winreg
        key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Uninstall\YiYou")
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "弈友 v2 围棋 AI 教练")
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                          str(inst / "卸载.bat"))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(inst / EXE_NAME))
        winreg.CloseKey(key)
    except OSError:
        pass


def message_box(text: str, title: str, flags: int) -> int:
    import ctypes
    return ctypes.windll.user32.MessageBoxW(0, text, title, flags)


def main() -> int:
    payload = payload_root()
    src = resource_path(EXE_NAME)
    if payload is None and not src.exists():
        if TEST_DIR:
            print("MISSING PAYLOAD")
            return 2
        message_box("安装包数据不完整，请重新下载。", APP_NAME, 0x10)
        return 2

    inst = install_dir()
    try:
        inst.mkdir(parents=True, exist_ok=True)
        dst = inst / EXE_NAME
        if payload is not None:
            shutil.copytree(payload, inst, dirs_exist_ok=True)
        else:
            shutil.copyfile(src, dst)
    except OSError as e:
        if TEST_DIR:
            print("COPY FAIL:", e)
            return 3
        message_box(f"安装失败：{e}", APP_NAME, 0x10)
        return 3

    if TEST_DIR:
        make_shortcut(dst, inst / "links" / "弈友.lnk", "test")
        files = sum(1 for _ in inst.rglob("*"))
        print(f"TEST INSTALL OK: {dst}（{'onedir 整目录' if payload else 'onefile 单文件'}，"
              f"{files} 项）")
        return 0

    make_shortcut(dst, desktop_dir() / "弈友.lnk", "弈友 v2 围棋 AI 教练")
    make_shortcut(dst, start_menu_dir() / "弈友.lnk", "弈友 v2 围棋 AI 教练")
    write_uninstaller(inst)
    write_uninstall_registry(inst)

    r = message_box(
        "弈友 v2 已安装完成！\n\n安装位置：%s\n\n是否立即启动？" % inst,
        APP_NAME, 0x44)  # MB_YESNO | MB_ICONINFORMATION
    if r == 6:  # IDYES
        subprocess.Popen([str(dst)], cwd=str(inst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
