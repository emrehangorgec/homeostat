from homeostat.device.adb import _is_link_error


def test_link_errors_are_recognised_in_any_case():
    assert _is_link_error("adb.EXE: device '0123abcd' not found")  # format captured on Windows
    assert _is_link_error("adb.exe: no devices/emulators found")
    assert _is_link_error("error: device offline")


def test_errors_of_the_device_command_are_not_link_errors():
    assert not _is_link_error("/system/bin/sh: pidof: not found")
    assert not _is_link_error("")
