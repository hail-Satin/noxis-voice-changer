import sys
import os

# Ensure the project root is on sys.path when running as a frozen exe
if getattr(sys, "frozen", False):
    sys.path.insert(0, sys._MEIPASS)

from gui.app import App


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
