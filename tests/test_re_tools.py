"""The RE tool server: parsers, evidence contract, and the refusal to escape.

The fixture binary is built byte by byte rather than compiled, so the tests are
about the parser and not about whichever gcc happens to be installed.
"""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctfagent import re_probe, re_tools
from ctfagent.re_tools import ReTools

TEXT = b"\x90\xc3"
DYNSTR = b"\x00__stack_chk_fail\x00printf\x00"
MESSAGE = b"flag{synthetic_elf}\x00"
SHSTRTAB = b"\x00.text\x00.dynstr\x00.dynsym\x00.shstrtab\x00"


def build_elf64() -> bytes:
    """A minimal but structurally valid ELF64: one PT_LOAD, a non-executable
    stack (so NX reads true), a .dynsym with __stack_chk_fail, and no .symtab."""
    ehsize, phentsize, shentsize = 64, 56, 64
    phoff = ehsize
    phnum = 2

    text_off = 0x100
    dynstr_off = text_off + len(TEXT)
    dynsym_off = dynstr_off + len(DYNSTR)
    message_off = dynsym_off + 24
    shstr_off = message_off + len(MESSAGE)
    shoff = (shstr_off + len(SHSTRTAB) + 7) & ~7

    names = {
        name: SHSTRTAB.index(name)
        for name in (b".text", b".dynstr", b".dynsym", b".shstrtab")
    }

    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    ehdr = struct.pack(
        "<16sHHIQQQIHHHHHH",
        ident, 3, 62, 1,           # e_type=DYN (PIE), e_machine=x86-64
        text_off, phoff, shoff, 0,
        ehsize, phentsize, phnum, shentsize, 5, 4,
    )

    load = struct.pack(
        "<IIQQQQQQ", 1, 5, 0, 0, 0, dynsym_off, dynsym_off, 0x1000
    )  # PT_LOAD, R+X
    stack = struct.pack(
        "<IIQQQQQQ", 0x6474E551, 6, 0, 0, 0, 0, 0, 0x10
    )  # PT_GNU_STACK, RW (no X) -> NX on
    phdrs = load + stack

    dynsym = struct.pack(
        "<IBBHQQ",
        DYNSTR.index(b"__stack_chk_fail"),
        (1 << 4) | 2,             # GLOBAL FUNC
        0, 0, 0, 0,
    )

    def shdr(name: bytes, stype: int, flags: int, addr: int, off: int, size: int,
             link: int = 0, info: int = 0, entsize: int = 0) -> bytes:
        return struct.pack(
            "<IIQQQQIIQQ",
            names[name], stype, flags, addr, off, size, link, info, 1, entsize,
        )

    sections = b"".join([
        b"\x00" * shentsize,                                       # SHT_NULL
        shdr(b".text", 1, 0x2 | 0x4, text_off, text_off, len(TEXT)),
        shdr(b".dynstr", 3, 0x2, dynstr_off, dynstr_off, len(DYNSTR)),
        shdr(b".dynsym", 11, 0x2, dynsym_off, dynsym_off, 24, link=2, info=1, entsize=24),
        shdr(b".shstrtab", 3, 0, shstr_off, shstr_off, len(SHSTRTAB)),
    ])

    body = bytearray(shoff + len(sections))
    body[0:ehsize] = ehdr
    body[phoff:phoff + len(phdrs)] = phdrs
    body[text_off:text_off + len(TEXT)] = TEXT
    body[dynstr_off:dynstr_off + len(DYNSTR)] = DYNSTR
    body[dynsym_off:dynsym_off + 24] = dynsym
    body[message_off:message_off + len(MESSAGE)] = MESSAGE
    body[shstr_off:shstr_off + len(SHSTRTAB)] = SHSTRTAB
    body[shoff:shoff + len(sections)] = sections
    return bytes(body)


@pytest.fixture()
def tools(tmp_path: Path) -> ReTools:
    work = tmp_path / "work"
    (work / "attachments").mkdir(parents=True)
    (work / "attachments" / "target").write_bytes(build_elf64())
    (work / "attachments" / "notes.txt").write_text(
        "the password is in the binary\n", encoding="utf-8"
    )
    (work / "attachments" / "runme.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    workspace = SimpleNamespace(
        root=work, work=work, attachments=work / "attachments"
    )
    return ReTools(SimpleNamespace(workspace=workspace), task_id=1)


def test_parser_reads_structure_and_mitigations(tools: ReTools) -> None:
    record = tools.call("re_inspect", {"path": "attachments/target"})
    result = record["normalized_result"]
    assert result["kind"] == "elf"
    assert result["class"] == "ELF64"
    assert result["machine"] == "x86-64"
    assert result["entry"] == 0x100
    assert result["mitigations"]["nx"] is True
    assert result["mitigations"]["canary"] is True
    assert result["stripped"] is True
    assert record["coverage"]["status"] == "complete"


def test_inventory_classifies_each_attachment(tools: ReTools) -> None:
    record = tools.call("re_inventory", {"path": "attachments"})
    kinds = {row["path"]: row["kind"] for row in record["normalized_result"]["entries"]}
    assert kinds["target"] == "elf"
    assert kinds["notes.txt"] == "unknown"
    assert kinds["runme.sh"] == "script"


def test_symbols_are_searchable(tools: ReTools) -> None:
    record = tools.call("re_symbols", {"path": "attachments/target", "query": "chk"})
    names = [s["name"] for s in record["normalized_result"]["symbols"]]
    assert names == ["__stack_chk_fail"]
    # A stripped binary cannot answer "what is this function called", and the
    # record has to say so rather than returning an empty list as a fact.
    assert record["coverage"]["status"] == "partial"
    assert any("剥离" in item for item in record["limitations"])


def test_strings_and_search_report_offsets(tools: ReTools) -> None:
    strings = tools.call("re_strings", {"path": "attachments/target"})
    assert any("__stack_chk_fail" in s["text"] for s in strings["normalized_result"]["strings"])

    found = tools.call(
        "re_search", {"path": "attachments/target", "pattern": "flag{", "mode": "ascii"}
    )
    matches = found["normalized_result"]["matches"]
    assert len(matches) == 1
    assert matches[0]["offset_hex"] == hex(matches[0]["offset"])
    assert found["coverage"]["status"] == "complete"

    missing = tools.call(
        "re_search", {"path": "attachments/target", "pattern": "nope", "mode": "ascii"}
    )
    assert missing["coverage"]["status"] == "complete"
    assert missing["residual_unknowns"], "an empty search must say what to check next"


def test_read_returns_hex_and_ascii(tools: ReTools) -> None:
    record = tools.call("re_read", {"path": "attachments/target", "offset": "0", "length": 16})
    result = record["normalized_result"]
    assert result["hex"].startswith("7f 45 4c 46")
    assert result["ascii"].startswith(".ELF")


def test_evidence_is_addressable(tools: ReTools) -> None:
    first = tools.call("re_inspect", {"path": "attachments/target"})
    again = tools.call("re_evidence_get", {"evidence_id": first["evidence_id"]})
    assert again == first
    assert tools.call("re_evidence_get", {"evidence_id": "re-nope"})["error"]


def test_non_binary_gets_an_honest_partial(tools: ReTools) -> None:
    record = tools.call("re_inspect", {"path": "attachments/runme.sh"})
    assert record["coverage"]["status"] == "partial"
    assert record["normalized_result"]["kind"] == "script"
    assert record["normalized_result"]["head"].startswith("#!/bin/sh")
    assert record["limitations"]


def test_paths_outside_the_workspace_are_refused(tools: ReTools) -> None:
    with pytest.raises(ValueError, match="工作区之外"):
        tools.call("re_inspect", {"path": "C:/Windows/win.ini"})


def test_a_refusal_becomes_a_failed_evidence_record() -> None:    # Over MCP the exception is caught and wrapped; the wrapper must still say
    # "failed", never hand back a bare {"error": ...} the model reads as "empty".
    record = re_tools._failure("re_inspect", "拒绝读取工作区之外的路径", "路径未被观测")
    assert record["coverage"]["status"] == "failed"
    assert record["limitations"] == ["拒绝读取工作区之外的路径"]
    assert record["residual_unknowns"] == ["路径未被观测"]
    assert record["evidence_id"].startswith("re-inspect-")


def test_truncation_is_flagged_not_silent() -> None:
    from ctfagent import re_tools as module

    original = module.MAX_RESULT_CHARS
    module.MAX_RESULT_CHARS = 80
    try:
        record = module._evidence("probe", "builtin", {"rows": ["x" * 200]})
    finally:
        module.MAX_RESULT_CHARS = original
    assert record["normalized_result"]["truncated"] is True
    assert record["coverage"]["status"] == "partial"


def test_capabilities_names_the_gaps(tools: ReTools) -> None:
    record = tools.call("re_capabilities", {})
    providers = {p["name"]: p for p in record["normalized_result"]["providers"]}
    assert "builtin" in providers
    assert record["limitations"], "missing tools must be listed, not implied"


def test_sniff_separates_java_from_fat_macho() -> None:
    java = b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\x00" * 8
    macho = b"\xca\xfe\xba\xbe\x00\x00\x00\x02" + b"\x00" * 8
    assert re_probe.sniff(java) == "java"
    assert re_probe.sniff(macho) == "macho"


def test_search_defaults_to_ascii(tools: ReTools) -> None:
    # Asking for "flag{" with the old hex default raised a cryptic fromhex
    # error; a string search is what callers mean almost every time.
    record = tools.call("re_search", {"path": "attachments/target", "pattern": "flag{"})
    assert record["normalized_result"]["mode"] == "ascii"
    assert record["normalized_result"]["matches"]
    specs = {spec["name"]: spec for spec in re_tools.tool_specs()}
    assert specs["re_search"]["input_schema"]["properties"]["mode"]["default"] == "ascii"


def test_undefined_import_is_not_an_address(tools: ReTools) -> None:
    # __stack_chk_fail lives in .dynsym with shndx 0 and value 0. Resolving it
    # to 0x0 made every "0x0" in the disassembly look like a cross-reference.
    parsed = re_probe.parse_elf(build_elf64())
    assert tools._address("__stack_chk_fail", parsed) is None
    assert tools._address(".text", parsed) == 0x100


def test_a_large_data_section_is_called_out() -> None:
    # A PyInstaller onefile bundle is 99% payload in a section the loader does
    # not execute. Stopping at "10 MB ELF, 37 KB of code" is how a worker
    # concludes the challenge is trivial when the logic is inside the archive.
    sections = [
        {"name": ".text", "offset": 0x1000, "size": 0x2000, "flags": "AX"},
        {"name": "pydata", "offset": 0x3000, "size": 4 * 1024 * 1024, "flags": "A"},
    ]
    segments = [{"offset": 0, "filesz": 0x3000}]
    payload = re_tools._embedded_payload(4 * 1024 * 1024 + 0x3000, sections, segments)
    assert payload is not None
    assert payload["large_data_sections"][0]["name"] == "pydata"
    assert re_tools._embedded_payload(0x3000, sections[:1], segments) is None


def test_a_packed_binary_says_the_code_is_a_stub() -> None:
    # Packed binaries are structurally valid ELF, so nothing else in the record
    # warns that the disassembly is the decompressor and not the program.
    assert re_tools._packer_marker(b"\x7fELF....$Info: This file is packed with the UPX")["name"] == "UPX"
    assert re_tools._packer_marker(b"\x7fELF....pyi-runtime-tmpdir")["name"] == "PyInstaller"
    assert re_tools._packer_marker(b"\x7fELF" + b"\x00" * 64) is None
