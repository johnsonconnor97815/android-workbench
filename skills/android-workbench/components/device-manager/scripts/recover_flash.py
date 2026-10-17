#!/usr/bin/env python3
"""Reconcile a partial partition write only after confirming bootloader reachability."""

from recover_flash_factory import main


if __name__ == "__main__":
    raise SystemExit(main({"device_manager.flash", "device_manager.recover_flash"}))
