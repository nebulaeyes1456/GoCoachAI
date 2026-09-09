; 弈友 GoCoachAI Windows 安装器脚本（Inno Setup 6）
; 用法：安装 Inno Setup 6 后，改好下方路径，右键本文件 → Compile
; 产物：dist/installer/弈友-Setup-x.y.z.exe
; 行为：把 dist/弈友（PyInstaller onedir 产物）安装到 Program Files，
;       创建开始菜单/桌面快捷方式、卸载器、.sgf 文件关联；
;       用户数据（题库/配置/日志/数据库）落在 %APPDATA%\GoCoachAI，卸载不丢。

#define AppName "弈友"
#define AppVersion "0.9.0"
#define AppPublisher "GoCoachAI"
#define AppExeName "弈友.exe"

; TODO: 按本机实际路径调整（打包机与分发机路径不同）
#define SourceDir "C:\Users\31878\Documents\GoCoachAI2\dist\弈友"
#define OutDir "C:\Users\31878\Documents\GoCoachAI2\dist\installer"

[Setup]
AppId={{B4A1D7E2-9F3C-4E5A-8B1D-1C2E3F4A5B6C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile={#SourceDir}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl"

[Files]
; PyInstaller onedir 整目录安装（_internal 含 backend/frontend/engine 等）
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Registry]
; .sgf 文件关联（可选：双击棋谱直接打开弈友）
Root: HKCU; Subkey: "Software\Classes\.sgf"; ValueType: string; ValueData: "GoCoachAI.SGF"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\GoCoachAI.SGF"; ValueType: string; ValueData: "围棋棋谱"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\GoCoachAI.SGF\DefaultIcon"; ValueType: string; ValueData: "{app}\{#AppExeName},0"
Root: HKCU; Subkey: "Software\Classes\GoCoachAI.SGF\shell\open\command"; ValueType: string; ValueData: """{app}\{#AppExeName}"" ""%1"""

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时只删程序目录；用户数据（%APPDATA%\GoCoachAI）保留
Type: filesandordirs; Name: "{app}"
