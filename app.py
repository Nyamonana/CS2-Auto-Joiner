from pathlib import Path
import sys

from cs2_joiner.gui import main


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--build-check":
        from cs2_joiner.build_check import run_build_check
        raise SystemExit(run_build_check(Path(sys.argv[2])))
    else:
        main()
