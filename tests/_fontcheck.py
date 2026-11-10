"""Does Qt see the *system* fonts, using the real windows platform?

_testfonts.boot() forces the offscreen platform, and Qt 6 no longer ships
fonts, so under the test harness QFontDatabase reports zero families. That is
an artefact of the harness, not of the machine. This script leaves the platform
alone and asks the real font database, so a candidate added to theme.py can be
verified against what the running app will actually get.

Run: .venv\\Scripts\\python.exe _fontcheck.py
"""
import os
import sys

# Deliberately NOT importing _testfonts: it would pin QT_QPA_PLATFORM=offscreen
# and hide every system font.
from PyQt6.QtGui import QFontDatabase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtWidgets import QApplication

from sshclient.core import fonts

app = QApplication(sys.argv)


families = QFontDatabase.families()
print(f"Qt sees {len(families)} families (platform: "
      f"{app.platformName()})")

mono_like = sorted(f for f in families if QFontDatabase.isFixedPitch(f))
print(f"\nfixed-pitch families ({len(mono_like)}):")
for f in mono_like:
    print(f"  {f}")

print("\nprobe:")
for name in ("Ubuntu Mono", "Consolas", "Cascadia Mono", "Cascadia Code",
             "JetBrains Mono", "Sarasa Mono SC", "Courier New", "Menlo",
             "DejaVu Sans Mono", "Noto Sans Mono"):
    exact = name in families
    fuzzy = [f for f in families if name.split()[0].lower() in f.lower()][:3]
    print(f"  {name:<18} {'exact match' if exact else 'no exact'}"
          f"{'  near: ' + ', '.join(fuzzy) if fuzzy else ''}")

print("\ncurrent candidate resolution:")
print(f"  UI   {fonts._UI_CANDIDATES}")
print(f"  mono {fonts._MONO_CANDIDATES}")
print(f"  UI   -> {fonts._first_available(fonts._UI_CANDIDATES)!r}")
print(f"  mono -> {fonts._first_available(fonts._MONO_CANDIDATES)!r}")

f = fonts.mono_font(11)
print(f"\nmono_font(11) actually resolves to: {f.family()!r} "
      f"fixedPitch={f.fixedPitch()}")
f2 = fonts.ui_font(10)
print(f"ui_font(10)   actually resolves to: {f2.family()!r}")
