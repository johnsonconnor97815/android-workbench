#!/usr/bin/env python3
"""Reconcile an interrupted OTA's selected recovery transport without retrying it."""
from recover_install_rom import main

if __name__ == "__main__":
    raise SystemExit(main(ota=True))
