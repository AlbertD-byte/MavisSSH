"""Can an unhandled exception in a Qt slot be caught instead of aborting?

In PyQt6 an exception escaping a slot calls qFatal() and the process dies
with STATUS_STACK_BUFFER_OVERRUN -- no traceback dialog, no chance to recover.
This checks whether sys.excepthook intercepts it, because if it does, the app
can install a safety net that reports the error instead of vanishing.

Run: .venv\\Scripts\\python.exe _excepthook_probe.py [plain|hook]
"""
import sys

from PyQt6 import QtCore, QtWidgets

mode = sys.argv[1] if len(sys.argv) > 1 else "plain"
app = QtWidgets.QApplication(sys.argv)

seen = []


def hook(exc_type, exc, tb):
    seen.append(exc_type.__name__)
    sys.__excepthook__(exc_type, exc, tb)


if mode == "hook":
    sys.excepthook = hook


class Boom(QtCore.QObject):
    def slot(self):
        raise ValueError("deliberate failure inside a slot")


b = Boom()
QtCore.QTimer.singleShot(0, b.slot)
QtCore.QTimer.singleShot(400, app.quit)
app.exec()

print(f"mode={mode}  excepthook saw: {seen or 'nothing'}")
print("process survived" if True else "")
sys.exit(0 if mode == "hook" and seen else (0 if mode == "plain" else 1))
