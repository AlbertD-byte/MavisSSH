Unicode true

; MavisSSH installer (NSIS script).
;
; IMPORTANT -- this file MUST stay pure ASCII:
;   `Unicode true` at line 1 makes makensis read *instructions* as UTF-8, but
;   Chinese characters inside comments still fail the ANSI codepage check.
;   On a zh-CN machine (ACP=936) that surfaces as
;       Bad text encoding: installer/mavisssh.nsi:NN
;   The line number points at the offending comment, NOT at a syntax error --
;   easy to misread as "the script is broken". All user-facing Chinese lives
;   in LangString below (UTF-16 resources, never touched by the ACP check).
;
; Two steps:
;     python installer/build.py --onefile       # produces dist\MavisSSH.exe
;     makensis installer\mavisssh.nsi          # produces the setup wizard
; or in one go (needs makensis on PATH):
;     python installer/build.py --onefile --nsis
;
; NSIS only builds Windows installers, and only on Windows. No cross-build.
;
; Why wrap the *onefile* exe rather than shipping the onedir tree:
;   onedir is dist\MavisSSH\ (exe + _internal\, ~100MB, thousands of files).
;   An installer for that needs thousands of [Files] entries: slow to install,
;   slow to uninstall, and a cancelled run leaves a half-populated directory.
;   onefile is ONE file -- a single [Files] entry, and "cancel" just means
;   "nothing was installed". The runtime cost (unpacking Qt on every launch)
;   is a launch-time concern, not an install-time one.
;
; Version lives here AND in sshclient\__init__.py. Both must be bumped by hand;
; the packer never reads Python source.

!include "MUI2.nsh"

; ---------- version (keep in sync with sshclient\__init__.py) ----------
!define APP_VERSION   "0.2.0"
!define APP_NAME      "MavisSSH"
!define APP_EXE       "MavisSSH.exe"

!define OUT_FILE      "..\dist\${APP_NAME}-${APP_VERSION}-Setup.exe"

; ============================================================
; Per-user install
; ============================================================
; RequestExecutionLevel user (not admin): MavisSSH is a personal tool, so
; %LOCALAPPDATA%\Programs needs no elevation and no UAC prompt. The trade-off
; is no "install for all users" -- for that, switch InstallDir to
; $PROGRAMFILES and this line to admin.
!define BUILD_DIR     "$LOCALAPPDATA\Programs\${APP_NAME}"

Name        "${APP_NAME} ${APP_VERSION}"
OutFile     "${OUT_FILE}"
InstallDir  "${BUILD_DIR}"
InstallDirRegKey HKCU "Software\${APP_NAME}" "InstallDir"
RequestExecutionLevel user
ShowInstDetails show
ShowUninstDetails show

; ============================================================
; Wizard pages
; ============================================================
!define MUI_ABORTWARNING
!define MUI_ICON      "icons\mavisssh.ico"
!define MUI_UNICON    "icons\mavisssh.ico"

!insertmacro MUI_PAGE_WELCOME
; Do NOT disable the directory page -- "install to a chosen directory" is
; the entire point of this change.
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
; License page: PyQt6 is GPLv3, so this project MUST ship as GPLv3. Showing
; the license text at install time satisfies GPL section 4 ("give all
; recipients a copy of this License"). Do not remove unless the project
; moves off PyQt6 to an LGPL Qt binding.
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; Chinese + English; NSIS picks by system language automatically.
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

; ============================================================
; Install
; ============================================================

; Desktop shortcut is an *optional component* the user can untick.
; Section name must be a plain literal: section headers are evaluated as the
; file is read, before the LangString definitions at the bottom exist.
; The localized text lives in MUI_DESCRIPTION_TEXT instead.
Section "Desktop shortcut" SecDesktopIcon
    CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
    WriteRegStr HKCU "Software\${APP_NAME}" "DesktopIcon" "1"
SectionEnd

Section "Program files" SecMain
    SetOutPath "$INSTDIR"

    ; A single source file. Do NOT add a bin\ directory: the onefile runtime
    ; already unpacks itself into %TEMP%, and the rz/sz inside _MEIPASS are
    ; the copies actually in use.
    File "..\dist\${APP_EXE}"

    ; ---- GPL s4: ship the license text with the binaries ----
    ; MUI_PAGE_LICENSE above shows it once, inside the wizard. After the
    ; install that page is gone, and whoever received the installer may
    ; never have read it. Dropping the license files into the install
    ; directory is what actually discharges the obligation.
    ; /nonfatal so a trimmed source tree still installs -- but then the
    ; package is no longer compliant, which the README says out loud.
    File "..\LICENSE"
    File /nonfatal "..\THIRD_PARTY_NOTICES.md"
    File /nonfatal "..\LICENSES\*.txt"

    ; ---- write uninstall.exe ----
    ; **This line is mandatory and easy to miss.** Without it makensis still
    ; reports success, but emits warning 6020 ("WriteUninstaller never used")
    ; and the install produces NO uninstaller at all: no Add/Remove Programs
    ; entry that works, and the "Uninstall" Start Menu shortcut points at a
    ; file that was never created. It does not fail the build -- it just
    ; silently ships an installer you cannot remove.
    ;
    ; Written before the shortcut below so the .lnk never points at a file
    ; that has not been created yet.
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; ---- Start Menu: launch + uninstall ----
    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\$(LNG_UNINSTN) ${APP_NAME}.lnk" "$INSTDIR\uninstall.exe"

    ; ---- remember where we installed, for upgrades and for uninstall ----
    WriteRegStr HKCU "Software\${APP_NAME}" "InstallDir" "$INSTDIR"

    ; ---- Add/Remove Programs entry ----
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "DisplayName" "${APP_NAME} ${APP_VERSION}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "DisplayIcon" "$INSTDIR\${APP_EXE}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "UninstallString" '"$INSTDIR\uninstall.exe"'
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "InstallLocation" "$INSTDIR"
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "NoModify" 1
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "NoRepair" 1
SectionEnd

; Section execution order is not the declaration order, so declare the body
; first -- if it fails we should not already have created a shortcut.
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "$(DESC_SECMAIN)"
    !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktopIcon} "$(DESC_SECDESKTOP)"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ============================================================
; Uninstaller
; ============================================================
Section "uninstall"
    ; Registry FIRST, directory SECOND. The other order leaves an uninstall
    ; entry pointing at a deleted file, and Add/Remove Programs errors out.
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
    DeleteRegValue HKCU "Software\${APP_NAME}" "DesktopIcon"
    DeleteRegKey HKCU "Software\${APP_NAME}"

    ; Delete only our own shortcut; never scan $DESKTOP (that would nuke a
    ; same-named file the user created themselves).
    Delete "$DESKTOP\${APP_NAME}.lnk"
    RMDir /r "$SMPROGRAMS\${APP_NAME}"

    ; RMDir /r on $INSTDIR is correct -- the dir holds only this exe plus
    ; uninstall.exe. If user data ever lands here (e.g. moving ~/.ssh in),
    ; switch this to deleting a fixed list of files instead.
    RMDir /r "$INSTDIR"
SectionEnd

; ============================================================
; Component descriptions -- ASCII only, see the note at the top.
;
; The wizard FRAMEWORK is still fully Chinese: `!insertmacro MUI_LANGUAGE
; "SimpChinese"` loads NSIS's own SimpChinese.isl, which is a UTF-16 resource,
; so welcome/directory/buttons/license page all render in Chinese on a
; zh-CN system. Only these two hand-written strings cannot be Chinese --
; `Unicode true` covers instruction parsing but NOT LangString values,
; which are validated against the ANSI codepage (936 on zh-CN) and abort the
; build with `Bad text encoding: <file>:NN`.
;
; Do not "fix" this by pasting Chinese in here: it breaks the build on any
; zh-CN machine, and the error's line number points at the string, which
; reads like a syntax error rather than an encoding one.
; ============================================================
LangString DESC_SECDESKTOP ${LANG_SIMPCHINESE} "Desktop shortcut (${APP_NAME}.lnk)"
LangString DESC_SECDESKTOP ${LANG_ENGLISH}   "Desktop shortcut (${APP_NAME}.lnk)"

LangString DESC_SECMAIN   ${LANG_SIMPCHINESE} "Program files and Start Menu entries"
LangString DESC_SECMAIN   ${LANG_ENGLISH}   "Program files and Start Menu entries"

LangString LNG_UNINSTN    ${LANG_SIMPCHINESE} "Uninstall"
LangString LNG_UNINSTN    ${LANG_ENGLISH}   "Uninstall"
