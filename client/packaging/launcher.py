import os
import sys

from qa_util.gui import main


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    main()
