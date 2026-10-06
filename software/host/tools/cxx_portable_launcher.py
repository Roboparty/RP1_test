#!/usr/bin/env python3
"""Strip host-specific CPU flags from the factory motors release build."""

from __future__ import annotations

import os
import sys


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("compiler launcher did not receive a compiler command")
    compiler, *arguments = sys.argv[1:]
    portable = [
        argument
        for argument in arguments
        if argument != "-march=native" and argument != "-mtune=native"
    ]
    portable.extend(("-march=x86-64", "-mtune=generic"))
    os.execvp(compiler, [compiler, *portable])


if __name__ == "__main__":
    main()
