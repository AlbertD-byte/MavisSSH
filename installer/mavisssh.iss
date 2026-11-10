; MavisSSH Inno Setup 脚本
;
; ⚠ 已废弃（2026-10-13）：改用同目录的 mavisssh.nsi（NSIS，开源免费）。
;    build.py 的 --nsis 只认 .nsi，这个文件不参与任何构建流程。
;    留在这里只是留个切换路径 —— 以后若想换回 Inno Setup，需要先：
;      1) 装 Inno Setup 6（https://jrsoftware.org/isdl.php）
;      2) 在 build.py 里把 --nsis 分支改回调 iscc
;    下面是废弃时点的原样内容，**未针对本仓库现状校验过**（引用了当时
;    尚不存在的 ..\LICENSE，该页现在已补上）。
;    **唯一的例外**：MyAppURL 原先是占位的假地址 "github.com/mavisssh"，
;    已改成实际仓库地址 —— 假地址留着会在有人真用这个脚本时把安装向导
;    的「项目主页」按钮指到一个不存在的站点。
;
; 打包步骤：
;   1. python installer/build.py --onefile          # 产出 dist\MavisSSH.exe
;   2. iscc installer/mavisssh.iss                 # 产出 dist\MavisSSH-Setup.exe
;   或：python installer/build.py --inno            # 上面两步合一（自动调 iscc）
;
; **不能交叉编译** —— .iss 只能在本机跑，产出也只能是本机格式。
; Windows 安装包必须跑在 Windows 上打，macOS/Linux 同理。

#define MyAppName     "MavisSSH"
#define MyAppVersion  "0.2.0"
#define MyAppPublisher   "MavisSSH"
#define MyAppURL          "https://github.com/AlbertD-byte/MavisSSH"
#define MyAppExeName      "MavisSSH.exe"
#define MyAppAssocName    "SSH Connection"

[Setup]
; ---------- 基础信息 ----------
AppId={{7b3e4a1c-8d2f-4e5a-b6c9-0d1f2e3a4b5c}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
; ---------- 输出 ----------
OutputDir=..\dist
OutputBaseFilename=MavisSSH-{#MyAppVersion}-Setup
; ---------- 压缩 ----------
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
; ---------- 权限/兼容 ----------
PrivilegesRequired=lowest
; Windows 10 1903+ 视觉风格（manifest）
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=no
WizardStyle=modern
; ---------- 图标 ----------
SetupIconFile=icons\mavisssh.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; ---------- 杂项 ----------
SignTool=
DisableStartupMessages=yes
; 64bit 仅打包 64bit（PyInstaller 产物本身就是平台相关的）
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; **整个 onefile EXE 打包进去**
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; 开始菜单 → 程序组
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
; 桌面（可选，用户取消勾选就不装）
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时把整个安装目录清干净（含用户的配置/日志）
Type: filesandordirs; Name: "{app}"
