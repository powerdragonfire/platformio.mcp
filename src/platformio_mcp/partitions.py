"""ESP32 partition tables: parse the CSV and binary forms, check them, and diff them.

Pure functions only, so they are unit-tested on captured samples. The binary layout follows
ESP-IDF's gen_esp32part.py: 32-byte entries (magic 0xAA50, type, subtype, offset, size, 16-byte
label, flags), an optional MD5 entry (magic 0xEBEB), and 0xFF padding to the end.
"""

from __future__ import annotations

import struct
from dataclasses import asdict, dataclass

ENTRY_MAGIC = b"\xaa\x50"
MD5_MAGIC = b"\xeb\xeb"
ENTRY_FMT = "<2sBBLL16sL"
ENTRY_SIZE = 32
TABLE_OFFSET = 0x8000
TABLE_SIZE = 0xC00
APP_ALIGN = 0x10000
DATA_ALIGN = 0x1000

TYPES = {"app": 0x00, "data": 0x01}
APP_SUBTYPES = {"factory": 0x00, "test": 0x20, **{f"ota_{i}": 0x10 + i for i in range(16)}}
DATA_SUBTYPES = {
    "ota": 0x00, "phy": 0x01, "nvs": 0x02, "coredump": 0x03, "nvs_keys": 0x04, "efuse": 0x05, "undefined": 0x06,
    "esphttpd": 0x80, "fat": 0x81, "spiffs": 0x82, "littlefs": 0x83,
}
FLAGS = {"encrypted": 0x1, "readonly": 0x2}


@dataclass
class Partition:
    name: str
    type: int
    subtype: int
    offset: int
    size: int
    flags: int = 0

    @property
    def end(self) -> int:
        return self.offset + self.size

    @property
    def type_name(self) -> str:
        return next((k for k, v in TYPES.items() if v == self.type), f"0x{self.type:02x}")

    @property
    def subtype_name(self) -> str:
        table = APP_SUBTYPES if self.type == TYPES["app"] else DATA_SUBTYPES if self.type == TYPES["data"] else {}
        return next((k for k, v in table.items() if v == self.subtype), f"0x{self.subtype:02x}")

    @property
    def is_app(self) -> bool:
        return self.type == TYPES["app"]

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update({"type": self.type_name, "subtype": self.subtype_name, "offset_hex": f"0x{self.offset:x}", "size_hex": f"0x{self.size:x}", "end_hex": f"0x{self.end:x}"})
        d["flags"] = [k for k, v in FLAGS.items() if self.flags & v]
        return d


def parse_number(text: str) -> int:
    """'0x10000', '64K', '1M', '65536' -> int, like gen_esp32part.py."""
    t = text.strip().lower()
    if not t:
        raise ValueError("empty number")
    mult = 1
    if t.endswith("k"):
        mult, t = 1024, t[:-1]
    elif t.endswith("m"):
        mult, t = 1024 * 1024, t[:-1]
    return int(t, 0) * mult


def _lookup(table: dict[str, int], value: str, what: str) -> int:
    v = value.strip().lower()
    if v in table:
        return table[v]
    try:
        return int(v, 0)
    except ValueError as exc:
        raise ValueError(f"unknown {what} '{value}'") from exc


def parse_partition_csv(text: str) -> list[Partition]:
    """Parse an ESP-IDF partitions CSV. Blank offsets are auto-assigned to the next aligned address."""
    parts: list[Partition] = []
    last_end = TABLE_OFFSET + TABLE_SIZE
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 5:
            raise ValueError(f"partition CSV line {lineno} needs at least 5 fields (name, type, subtype, offset, size): {raw!r}")
        name, type_s, subtype_s, offset_s, size_s = fields[:5]
        flags_s = fields[5] if len(fields) > 5 else ""
        ptype = _lookup(TYPES, type_s, "type")
        subtable = APP_SUBTYPES if ptype == TYPES["app"] else DATA_SUBTYPES
        subtype = _lookup(subtable, subtype_s, "subtype") if subtype_s else 0
        size = parse_number(size_s)
        align = APP_ALIGN if ptype == TYPES["app"] else DATA_ALIGN
        if offset_s:
            offset = parse_number(offset_s)
        else:
            offset = (last_end + align - 1) // align * align
        flags = 0
        for f in flags_s.replace(":", " ").split():
            flags |= FLAGS.get(f.strip().lower(), 0)
        parts.append(Partition(name=name[:16], type=ptype, subtype=subtype, offset=offset, size=size, flags=flags))
        last_end = offset + size
    return parts


def parse_partition_bin(data: bytes) -> list[Partition]:
    """Parse the binary table as flashed at 0x8000. Stops at the MD5 entry or the first 0xFF entry."""
    parts: list[Partition] = []
    for i in range(0, min(len(data), TABLE_SIZE) - ENTRY_SIZE + 1, ENTRY_SIZE):
        chunk = data[i : i + ENTRY_SIZE]
        if chunk[:2] == MD5_MAGIC or chunk == b"\xff" * ENTRY_SIZE:
            break
        magic, ptype, subtype, offset, size, label, flags = struct.unpack(ENTRY_FMT, chunk)
        if magic != ENTRY_MAGIC:
            raise ValueError(f"no partition table magic at byte {i} (found {chunk[:2].hex()}); the flash region may be erased or hold a different image.")
        parts.append(Partition(name=label.split(b"\x00", 1)[0].decode("utf-8", "replace"), type=ptype, subtype=subtype, offset=offset, size=size, flags=flags))
    return parts


def to_binary(parts: list[Partition], with_md5: bool = True) -> bytes:
    """Encode like gen_esp32part.py (used by tests and by the device diff)."""
    import hashlib

    out = b"".join(struct.pack(ENTRY_FMT, ENTRY_MAGIC, p.type, p.subtype, p.offset, p.size, p.name.encode()[:16].ljust(16, b"\x00"), p.flags) for p in parts)
    if with_md5:
        out += MD5_MAGIC + b"\xff" * 14 + hashlib.md5(out).digest()
    return out.ljust(TABLE_SIZE, b"\xff")


def check_partitions(parts: list[Partition], flash_size: int | None = None, app_size: int | None = None) -> list[dict]:
    """Return structured issues: {severity: error|warning|info, code, message, fix}."""
    issues: list[dict] = []

    def add(severity: str, code: str, message: str, fix: str) -> None:
        issues.append({"severity": severity, "code": code, "message": message, "fix": fix})

    if not parts:
        add("error", "empty_table", "The partition table has no entries.", "Point board_build.partitions at a valid CSV or remove it to use the framework default.")
        return issues
    for p in parts:
        align = APP_ALIGN if p.is_app else DATA_ALIGN
        if p.offset % align:
            add("error", "misaligned", f"{p.name} at 0x{p.offset:x} is not aligned to 0x{align:x} ({'app' if p.is_app else 'data'} partitions must be).", f"Move {p.name} to 0x{(p.offset + align - 1) // align * align:x}.")
        if p.offset < TABLE_OFFSET + TABLE_SIZE:
            add("error", "overlaps_table", f"{p.name} starts at 0x{p.offset:x}, inside the bootloader/partition-table area (ends 0x{TABLE_OFFSET + TABLE_SIZE:x}).", "Start the first partition at 0x9000 or later.")
    ordered = sorted(parts, key=lambda p: p.offset)
    for a, b in zip(ordered, ordered[1:]):
        if b.offset < a.end:
            add("error", "overlap", f"{a.name} (0x{a.offset:x}-0x{a.end:x}) overlaps {b.name} (0x{b.offset:x}-0x{b.end:x}).", f"Shrink {a.name} or move {b.name} to 0x{a.end:x} or later.")
    table_end = max(p.end for p in parts)
    if flash_size:
        if table_end > flash_size:
            add("error", "exceeds_flash", f"Partitions end at 0x{table_end:x} ({table_end / 1048576:.2f} MB) but the flash is {flash_size / 1048576:.0f} MB (0x{flash_size:x}).", "Pick a CSV sized for this flash (e.g. default.csv for 4 MB, default_16MB.csv for 16 MB) or set board_upload.flash_size to the real chip size.")
        elif flash_size - table_end >= 1024 * 1024:
            add("info", "unused_flash", f"{(flash_size - table_end) / 1048576:.1f} MB of flash after 0x{table_end:x} is not assigned to any partition.", "Enlarge the app or data partitions, or switch to a CSV that uses the full flash.")
    apps = [p for p in parts if p.is_app]
    ota_slots = [p for p in apps if 0x10 <= p.subtype < 0x20]
    otadata = [p for p in parts if p.type == TYPES["data"] and p.subtype == DATA_SUBTYPES["ota"]]
    if not apps:
        add("error", "no_app", "No app partition (factory or ota_N); nothing can boot.", "Add 'app0, app, ota_0, 0x10000, 0x140000' or a factory partition.")
    if ota_slots and not otadata:
        add("error", "ota_without_otadata", f"{len(ota_slots)} OTA app slot(s) but no 'otadata' partition; the bootloader cannot pick a slot and OTA updates will not activate.", "Add 'otadata, data, ota, 0xe000, 0x2000'.")
    if otadata and not ota_slots:
        add("warning", "otadata_without_ota", "An otadata partition exists but there is only a factory app; OTA updates have nowhere to go.", "Add ota_0/ota_1 slots or drop otadata.")
    if len(ota_slots) == 1:
        add("warning", "single_ota_slot", "Only one OTA slot; an interrupted update leaves the device unbootable.", "Add a second ota_N partition of the same size, or use a factory partition as the fallback.")
    if ota_slots:
        sizes = {p.size for p in ota_slots}
        if len(sizes) > 1:
            add("warning", "uneven_ota_slots", "OTA slots differ in size: " + ", ".join(f"{p.name}=0x{p.size:x}" for p in ota_slots) + ". The firmware must fit the smallest slot.", "Give every ota_N slot the same size.")
    if app_size is not None and apps:
        smallest = min(apps, key=lambda p: p.size)
        pct = round(100 * app_size / smallest.size, 1)
        if app_size > smallest.size:
            add("error", "app_too_big", f"firmware.bin is {app_size:,} B but the smallest app slot {smallest.name} holds {smallest.size:,} B ({pct}%); upload will fail or the device will not boot.", "Use a CSV with a larger app slot (huge_app.csv, min_spiffs.csv, no_ota.csv) or shrink the firmware (pio_size_report).")
        elif pct >= 90:
            add("warning", "app_nearly_full", f"firmware.bin uses {pct}% of app slot {smallest.name} ({app_size:,} of {smallest.size:,} B).", "Leave headroom for OTA growth: a bigger slot CSV or a smaller firmware.")
        else:
            add("info", "app_fits", f"firmware.bin uses {pct}% of app slot {smallest.name} ({app_size:,} of {smallest.size:,} B).", "")
    if not any(p.type == TYPES["data"] and p.subtype == DATA_SUBTYPES["coredump"] for p in parts):
        add("info", "no_coredump", "No coredump partition; crashes leave no core dump for pio_coredump to read (serial backtraces still work).", "Add 'coredump, data, coredump, , 0x10000' and enable core dump to flash in the sdkconfig if you want post-mortem dumps.")
    if not any(p.type == TYPES["data"] and p.subtype == DATA_SUBTYPES["nvs"] for p in parts):
        add("warning", "no_nvs", "No nvs partition; WiFi/BT calibration and Preferences have nowhere to persist.", "Add 'nvs, data, nvs, 0x9000, 0x5000'.")
    return issues


def diff_partitions(expected: list[Partition], actual: list[Partition]) -> list[dict]:
    """Compare two tables by name; each difference says which field disagrees."""
    diffs: list[dict] = []
    exp = {p.name: p for p in expected}
    act = {p.name: p for p in actual}
    for name in exp.keys() | act.keys():
        e, a = exp.get(name), act.get(name)
        if e is None:
            diffs.append({"name": name, "kind": "extra_on_device", "device": a.to_dict()})
        elif a is None:
            diffs.append({"name": name, "kind": "missing_on_device", "expected": e.to_dict()})
        else:
            fields = [f for f in ("type", "subtype", "offset", "size") if getattr(e, f) != getattr(a, f)]
            if fields:
                diffs.append({"name": name, "kind": "changed", "fields": fields, "expected": e.to_dict(), "device": a.to_dict()})
    order = {"missing_on_device": 0, "changed": 1, "extra_on_device": 2}
    diffs.sort(key=lambda d: (order[d["kind"]], d["name"]))
    return diffs
