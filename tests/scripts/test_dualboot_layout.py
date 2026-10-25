"""Dual-boot layout (docs/DUAL_BOOT.md): the partition CSV, the PlatformIO env and the PC-side scripts.

Pure host checks. Nothing here opens a serial port or runs esptool: the PowerShell scripts are only run
with -DryRun (or with arguments that make them stop before they reach a chip). Tests that need local
artefacts skip with a reason when they are absent: the PlatformIO framework (gen_esp32part.py,
boot_app0.bin), a built firmware.bin and a verified flash backup under _backups/ (local only, never
committed).
"""

from __future__ import annotations

import configparser
import glob
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parents[2]
FIRMWARE = REPO / "firmware"
CSV = FIRMWARE / "partitions_8mb_dualboot.csv"
SCRIPTS = REPO / "scripts"

FLASH_BYTES = 0x800000
OTADATA_OFFSET = 0xE000  # where every PlatformIO upload writes boot_app0.bin
IMAGE_MAGIC = 0xE9
APP_DESC_MAGIC = 0xABCD5432
APP_TYPE, DATA_TYPE = 0, 1
SUBTYPE_OTA_0, SUBTYPE_OTA_1, SUBTYPE_OTADATA = 0x10, 0x11, 0x00
OYSTER_FACTORY_BYTES = 0x500000  # partitions_8mb_noota.csv


class Part(NamedTuple):
    name: str
    type: int
    subtype: int
    offset: int
    size: int


def parse_table(blob: bytes, start: int = 0) -> list[Part]:
    """Entries of a binary ESP-IDF partition table (the MD5 row or a blank row ends it)."""
    parts = []
    for i in range(0, 0xC00, 32):
        row = blob[start + i : start + i + 32]
        if len(row) < 32 or row[:2] != b"\xaa\x50":
            break
        ptype, subtype, offset, size = struct.unpack("<BBII", row[2:12])
        parts.append(Part(row[12:28].split(b"\0")[0].decode("ascii"), ptype, subtype, offset, size))
    return parts


def framework_dir() -> Path | None:
    cores = [os.environ.get("PLATFORMIO_CORE_DIR", ""), str(Path.home() / ".platformio")]
    for core in cores:
        if core:
            p = Path(core) / "packages" / "framework-arduinoespressif32"
            if p.is_dir():
                return p
    return None


def need_framework_file(*rel: str) -> Path:
    fw = framework_dir()
    if fw is None:
        pytest.skip("the PlatformIO Arduino framework is not installed (run scripts/setup.ps1)")
    path = fw.joinpath(*rel)
    if not path.is_file():
        pytest.skip(f"{path.name} not found in the PlatformIO Arduino framework")
    return path


def run_gen(csv: Path, out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    gen = need_framework_file("tools", "gen_esp32part.py")
    cmd = [sys.executable, "-I", str(gen), *extra, str(csv), str(out)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603


@pytest.fixture(scope="module")
def table(tmp_path_factory: pytest.TempPathFactory) -> list[Part]:
    out = tmp_path_factory.mktemp("parttable") / "partitions.bin"
    res = run_gen(CSV, out, "--flash-size", "8MB")
    assert res.returncode == 0, res.stderr
    return parse_table(out.read_bytes())


def by_name(parts: list[Part], name: str) -> Part:
    return next(p for p in parts if p.name == name)


# ---- the CSV ---------------------------------------------------------------------------------------------


def test_gen_esp32part_accepts_the_layout_for_8mb(table: list[Part]) -> None:
    assert [p.name for p in table] == ["nvs", "otadata", "ota_0", "ota_1", "spiffs", "coredump"]


def test_gen_esp32part_rejects_an_overlap(tmp_path: Path) -> None:
    """The validator is really enforcing: ota_1 moved into ota_0 must fail."""
    bad = tmp_path / "bad.csv"
    good = CSV.read_text(encoding="ascii")
    text = re.sub(r"(ota_1,\s+app,\s+ota_1,\s+)0x3B0000", r"\g<1>0x300000", good)
    assert text != good
    bad.write_text(text, encoding="ascii")
    res = run_gen(bad, tmp_path / "bad.bin", "--flash-size", "8MB")
    assert res.returncode != 0


def test_no_overlaps_alignment_and_total(table: list[Part]) -> None:
    ordered = sorted(table, key=lambda p: p.offset)
    assert ordered[0].offset >= 0x9000  # bootloader at 0x0, partition table at 0x8000 (4 KiB)
    for a, b in zip(ordered, ordered[1:], strict=False):
        assert a.offset + a.size <= b.offset, f"{a.name} overlaps {b.name}"
    for p in table:
        assert p.offset % 0x1000 == 0 and p.size % 0x1000 == 0, p.name
        if p.type == APP_TYPE:
            assert p.offset % 0x10000 == 0, f"{p.name}: app partitions are 64 KiB aligned"
    assert max(p.offset + p.size for p in table) <= FLASH_BYTES


def test_otadata_sits_on_the_boot_app0_offset_and_nvs_stays_below(table: list[Part]) -> None:
    ota = by_name(table, "otadata")
    nvs = by_name(table, "nvs")
    assert (ota.type, ota.subtype) == (DATA_TYPE, SUBTYPE_OTADATA)
    assert ota.offset == OTADATA_OFFSET and ota.size == 0x2000
    assert nvs.offset + nvs.size <= ota.offset
    assert (
        by_name(table, "ota_0").offset == ota.offset + ota.size
    )  # nothing between otadata and the first app


def test_ota_slots_are_ota_0_and_ota_1(table: list[Part]) -> None:
    assert (by_name(table, "ota_0").type, by_name(table, "ota_0").subtype) == (APP_TYPE, SUBTYPE_OTA_0)
    assert (by_name(table, "ota_1").type, by_name(table, "ota_1").subtype) == (APP_TYPE, SUBTYPE_OTA_1)
    assert not any(p.name == "factory" for p in table)


def firmware_bin_size() -> int | None:
    default = FIRMWARE / ".pio" / "build" / "cardputer-adv" / "firmware.bin"
    if default.is_file():
        return default.stat().st_size
    others = [
        Path(p) for p in glob.glob(str(FIRMWARE / ".pio" / "build*" / "cardputer-adv*" / "firmware.bin"))
    ]
    return max((p.stat().st_size for p in others), default=None)


def test_ota_0_has_headroom_over_the_current_oyster_image(table: list[Part]) -> None:
    size = firmware_bin_size()
    if size is None:
        pytest.skip("no built firmware.bin under firmware/.pio (build the cardputer-adv env first)")
    assert by_name(table, "ota_0").size >= size * 1.3, f"firmware.bin is {size} bytes"


# ---- the dump: what ota_1 has to hold ---------------------------------------------------------------


def original_dump() -> tuple[bytes, Part, Path] | None:
    """Newest verified backup that still holds a non-Oyster factory app: (dump, that partition, path)."""
    pattern = str(REPO / "_backups" / "*" / "*" / "factory-8MB.bin")
    for path in sorted(glob.glob(pattern), reverse=True):
        sidecar = Path(path + ".sha256")
        blob = Path(path).read_bytes()
        if len(blob) != FLASH_BYTES or not sidecar.is_file():
            continue
        if hashlib.sha256(blob).hexdigest() != sidecar.read_text(encoding="ascii").split()[0].lower():
            continue
        parts = parse_table(blob, 0x8000)
        factory = next((p for p in parts if p.type == APP_TYPE and p.subtype == 0), None)
        if factory is None or factory.size == OYSTER_FACTORY_BYTES or blob[0] != IMAGE_MAGIC:
            continue
        return blob, factory, Path(path)
    return None


def image_end(app: bytes) -> int:
    """End of an app image from its own headers: segments, aligned checksum byte, optional SHA-256."""
    pos = 24
    for _ in range(app[1]):
        pos += 8 + struct.unpack("<I", app[pos + 4 : pos + 8])[0]
    pos += 15 - (pos % 16)
    return pos + 1 + (32 if app[23] == 1 else 0)


def test_ota_1_holds_the_original_image_with_exact_bounds(table: list[Part]) -> None:
    found = original_dump()
    if found is None:
        pytest.skip(
            "no verified backup with the original firmware under _backups/ (scripts/firmware-backup.ps1)"
        )
    blob, factory, _ = found
    app = blob[factory.offset : factory.offset + factory.size]
    assert app[0] == IMAGE_MAGIC
    assert struct.unpack("<I", app[0x20:0x24])[0] == APP_DESC_MAGIC
    used = len(app.rstrip(b"\xff"))
    end = image_end(app)
    assert used <= end <= factory.size
    assert (
        end - used <= 32
    )  # only a SHA-256 ending in 0xFF bytes can make the last data byte lie before the image end
    cut = max(used, end)
    cut = -(-cut // 0x1000) * 0x1000  # rounded up to 4 KiB, as dualboot-setup.ps1 does
    assert cut <= by_name(table, "ota_1").size


# ---- boot_app0.bin and the bootloader's slot rule ---------------------------------------------------------


def esp_crc32_le(data: bytes, init: int = 0xFFFFFFFF) -> int:
    """ESP ROM crc32_le: the start value is inverted, so UINT32_MAX means 'start at 0' (not the zlib CRC)."""
    crc = ~init & 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return ~crc & 0xFFFFFFFF


def test_boot_app0_selects_ota_0_and_lands_on_otadata(table: list[Part]) -> None:
    boot = need_framework_file("tools", "partitions", "boot_app0.bin").read_bytes()
    ota = by_name(table, "otadata")
    assert len(boot) == ota.size  # 8 KiB, exactly the otadata partition
    seq0, state0, crc0 = struct.unpack("<I20xII", boot[:32])
    assert (seq0, state0) == (1, 0xFFFFFFFF)
    assert crc0 == esp_crc32_le(struct.pack("<I", seq0))
    slot = (seq0 - 1) % 2  # the bootloader: (sequence - 1) modulo the number of OTA slots
    assert slot == 0, "entry 0 must select ota_0, the first OTA app of the table"
    seq1 = struct.unpack("<I", boot[0x1000:0x1004])[0]
    assert seq1 < seq0, "the second entry must not outrank the first"


def test_platformio_writes_boot_app0_at_otadata() -> None:
    build = need_framework_file("tools", "platformio-build.py").read_text(encoding="utf-8")
    assert re.search(
        r'\("0xe000",\s*join\(FRAMEWORK_DIR,\s*"tools",\s*"partitions",\s*"boot_app0\.bin"\)\)', build
    )


def test_framework_keeps_app_rollback_enabled() -> None:
    """The return path (a reset drops an app that never marks itself valid) needs both options; a framework
    upgrade must not lose them unnoticed. Behaviour on the device is still unverified (docs/DUAL_BOOT.md)."""
    cfg = need_framework_file("tools", "sdk", "esp32s3", "sdkconfig").read_text(encoding="utf-8").splitlines()
    assert "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in cfg  # bootloader: NEW, PENDING_VERIFY, ABORTED
    assert "CONFIG_APP_ROLLBACK_ENABLE=y" in cfg  # the app: esp_ota_set_boot_partition writes state NEW


# ---- platformio.ini --------------------------------------------------------------------------------------


def test_platformio_env_and_untouched_default() -> None:
    cfg = configparser.ConfigParser(interpolation=None, strict=False, inline_comment_prefixes=(";",))
    cfg.read(FIRMWARE / "platformio.ini", encoding="utf-8")
    assert cfg["platformio"]["default_envs"].strip() == "cardputer-adv"
    assert cfg["env:cardputer-adv"]["board_build.partitions"].strip() == "partitions_8mb_noota.csv"
    dual = cfg["env:cardputer-adv-dualboot"]
    assert dual["extends"].strip() == "env:cardputer-adv"
    assert dual["board_build.partitions"].strip() == "partitions_8mb_dualboot.csv"
    assert set(dual) - set(cfg["DEFAULT"]) == {"extends", "board_build.partitions"}  # nothing else differs


# ---- the PowerShell scripts (only ever with -DryRun or with arguments that stop early) ---------------------

PWSH = shutil.which("pwsh") or shutil.which("powershell")
needs_pwsh = pytest.mark.skipif(
    sys.platform != "win32" or PWSH is None, reason="the scripts need Windows PowerShell or PowerShell 7"
)


def run_ps(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    cmd = [
        PWSH,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPTS / script),
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False, cwd=REPO)  # noqa: S603


@needs_pwsh
def test_setup_dry_run_cuts_the_same_bytes_as_python() -> None:
    found = original_dump()
    if found is None:
        pytest.skip("no verified backup with the original firmware under _backups/")
    blob, factory, path = found
    mac_dir = path.parent.parent.name
    app = blob[factory.offset : factory.offset + factory.size]
    cut = -(-max(len(app.rstrip(b"\xff")), image_end(app)) // 0x1000) * 0x1000
    expected = hashlib.sha256(app[:cut]).hexdigest()
    before = sorted(glob.glob(str(REPO / "_backups" / "*" / "dualboot" / "*")))

    res = run_ps("dualboot-setup.ps1", "-DryRun", "-Mac", mac_dir)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "DRY RUN" in res.stdout and "0x3B0000" in res.stdout
    assert f"sha256:   {expected}" in res.stdout
    assert f"cut:      0x{cut:X} bytes" in res.stdout
    assert (
        sorted(glob.glob(str(REPO / "_backups" / "*" / "dualboot" / "*"))) == before
    )  # a dry run creates nothing


@needs_pwsh
def test_setup_dry_run_wins_over_apply_and_never_names_a_chip_step_as_done() -> None:
    found = original_dump()
    if found is None:
        pytest.skip("no verified backup with the original firmware under _backups/")
    res = run_ps("dualboot-setup.ps1", "-DryRun", "-Apply", "-Mac", found[2].parent.parent.name)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "-DryRun wins over -Apply" in res.stdout
    assert "Writing 0x" not in res.stdout


@needs_pwsh
@pytest.mark.parametrize("script", ["dualboot-setup.ps1", "oyster-return.ps1"])
def test_scripts_need_a_port_unless_dry_run(script: str) -> None:
    res = run_ps(script)  # stops before any chip access
    assert res.returncode != 0
    assert "-Port is required" in res.stdout + res.stderr


@needs_pwsh
def test_return_dry_run_plans_the_otadata_write() -> None:
    res = run_ps("oyster-return.ps1", "-DryRun")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "DRY RUN" in res.stdout
    assert "write_flash" in res.stdout and "0xE000" in res.stdout
    assert "sequence 1 -> ota_0" in res.stdout


@needs_pwsh
def test_flash_script_takes_the_dualboot_env() -> None:
    if not (REPO / ".venv" / "Scripts" / "pio.exe").is_file():
        pytest.skip("PlatformIO is not installed in .venv (scripts/setup.ps1)")
    dual = run_ps("firmware-flash.ps1", "-Port", "COM9", "-Env", "cardputer-adv-dualboot", "-DryRun")
    assert dual.returncode == 0, dual.stdout + dual.stderr
    assert "pio run -e cardputer-adv-dualboot -t upload" in dual.stdout
    default = run_ps("firmware-flash.ps1", "-Port", "COM9", "-DryRun")
    assert default.returncode == 0, default.stdout + default.stderr
    assert "pio run -e cardputer-adv -t upload" in default.stdout
    bad = run_ps("firmware-flash.ps1", "-Port", "COM9", "-Env", "nonsense", "-DryRun")
    assert bad.returncode != 0
