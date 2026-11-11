"""`python -m auto_factory` -> co-supervisor CLI (see auto_factory.supervisor)."""
import sys

from .supervisor import main

if __name__ == "__main__":
    sys.exit(main())
