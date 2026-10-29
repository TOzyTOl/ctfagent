"""Binary reconnaissance that needs no decompiler.

REA's strongest idea is not a better disassembler — it is that every answer is
*evidence*: it says which provider produced it, what was covered, and what was
not. This module is the provider layer for that contract. The built-in parsers
(readelf-equivalent ELF, PE and Mach-O headers) always work; external tools
(``readelf``/``objdump``/``nm``/``file``) are enhancements whose absence is
reported rather than papered over.

Nothing here executes the target. Dynamic analysis stays the worker's job,
through Bash, where the guard hook still applies.
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import struct
import subprocess
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: Structural parsing reads the whole file; past this we read a prefix and say so.
MAX_READ = 256 * 1024 * 1024

ELF_TYPES = {0: "NONE", 1: "REL", 2: "EXEC", 3: "DYN", 4: "CORE"}
ELF_MACHINES = {
    3: "x86", 8: "MIPS", 20: "PowerPC", 21: "PowerPC64", 22: "S390",
    40: "ARM", 62: "x86-64", 183: "AArch64", 243: "RISC-V", 247: "BPF",
}
PE_MACHINES = {
    0x14C: "x86", 0x8664: "x86-64", 0x1C0: "ARM", 0xAA64: "AArch64",
    0x200: "IA-64", 0x5032: "RISC-V32", 0x5064: "RISC-V64",
}
MACHO_CPUS = {7: "x86", 0x01000007: "x86-64", 12: "ARM", 0x0100000C: "AArch64", 18: "PowerPC"}

SHT = {0: "NULL", 1: "PROGBITS", 2: "SYMTAB", 3: "STRTAB", 4: "RELA", 5: "HASH",
       6: "DYNAMIC", 7: "NOTE", 8: "NOBITS", 9: "REL", 11: "DYNSYM", 14: "INIT_ARRAY",
       15: "FINI_ARRAY", 16: "PREINIT_ARRAY", 17: "GROUP", 18: "SYMTAB_SHNDX"}
PT = {0: "NULL", 1: "LOAD", 2: "DYNAMIC", 3: "INTERP", 4: "NOTE", 6: "PHDR",
      7: "TLS", 0x6474E550: "GNU_EH_FRAME", 0x6474E551: "GNU_STACK",
      0x6474E552: "GNU_RELRO", 0x6474E553: "GNU_PROPERTY"}

PT_LOAD, PT_DYNAMIC, PT_INTERP, PT_GNU_STACK, PT_GNU_RELRO = 1, 2, 3, 0x6474E551, 0x6474E552
PF_X, PF_W, PF_R = 1, 2, 4
SHF_WRITE, SHF_ALLOC, SHF_EXECINSTR = 1, 2, 4
DT_NEEDED, DT_STRTAB, DT_SONAME, DT_RPATH, DT_RUNPATH = 1, 5, 14, 15, 29
DT_BIND_NOW, DT_FLAGS_1 = 24, 0x6FFFFFFB
DF_1_PIE, DF_1_NOW = 0x08000000, 0x1


# --------------------------------------------------------------------------- providers
@dataclass
class Provider:
    name: str
    kind: str          # binary | library | runtime
    purpose: str
    available: bool
    detail: str = ""


def _which(name: str) -> str:
    return shutil.which(name) or ""


def _version(exe: str) -> str:
    try:
        proc = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=10,
            errors="replace",
        )
        return (proc.stdout or proc.stderr).splitlines()[0].strip()[:120]
    except (OSError, subprocess.SubprocessError):
        return ""


def detect_providers() -> list[Provider]:
    """Capability report. Every entry says what it would add, not just whether
    the binary exists — a missing tool is only useful information with its gap."""
    out: list[Provider] = [
        # The built-in parsers are the baseline: they never go missing, so every
        # other provider is an enhancement whose absence is survivable.
        Provider("builtin", "parser",
                 "ELF/PE/Mach-O 头部、段表、符号、保护机制、字符串与字节搜索（不依赖外部工具）",
                 True, "内置"),
    ]

    tools = [
        ("readelf", "ELF 段表/符号/动态依赖/DWARF", "binutils"),
        ("objdump", "反汇编、重定位、DWARF 行表", "binutils"),
        ("nm", "符号列表", "binutils"),
        ("strings", "字符串提取（内置扫描器已覆盖）", "binutils"),
        ("size", "段体积概览", "binutils"),
        ("addr2line", "地址到源码行（需要调试信息）", "binutils"),
        ("file", "文件类型指纹", "file"),
        ("gdb", "动态调试、内存转储", "gdb"),
        ("docker", "在 Linux 容器里运行 ELF / 用镜像内工具", "docker"),
        ("r2", "交互式反汇编与函数图", "radare2"),
        ("rizin", "交互式反汇编与函数图", "rizin"),
        ("binwalk", "固件/镜像内嵌文件识别", "binwalk"),
        ("upx", "UPX 脱壳", "upx"),
        ("apktool", "APK 资源与 smali 反编译", "apktool"),
        ("jadx", "DEX 反编译为 Java", "jadx"),
        ("ilspycmd", ".NET 程序集反编译", "ilspy"),
    ]
    for name, purpose, family in tools:
        exe = _which(name)
        detail = _version(exe) if exe else ""
        out.append(Provider(name, family, purpose, bool(exe), detail or ("已安装" if exe else "PATH 里找不到")))

    libs = [
        ("pyelftools", "ELF 深度解析（DWARF、重定位）"),
        ("capstone", "指令级反汇编 API"),
        ("lief", "可执行文件改写/加壳分析"),
        ("z3", "符号求解，还原校验逻辑"),
        ("pwn", "pwntools：ELF 封装、ROP、远程交互"),
        ("unicorn", "指令级模拟执行"),
        ("angr", "符号执行"),
        ("frida", "运行时 hook"),
    ]
    for name, purpose in libs:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        out.append(Provider(name, "library", purpose, spec is not None,
                            "已安装" if spec else "未安装（可在工作区内自行 pip 安装）"))

    # Decompilers are checked by environment/dir, not by PATH name.
    import os
    ghidra = os.environ.get("GHIDRA_HOME") or os.environ.get("GHIDRA_INSTALL_DIR") or ""
    out.append(Provider("ghidra", "decompiler", "反编译出伪代码", bool(ghidra and Path(ghidra).is_dir()),
                        ghidra or "未配置 GHIDRA_HOME"))
    return out


def capabilities_report() -> dict[str, Any]:
    providers = [asdict(p) for p in detect_providers()]
    missing = [p["name"] for p in providers if not p["available"]]
    notes: list[str] = []
    have = {p["name"] for p in providers if p["available"]}
    if not ({"ghidra"} & have):
        notes.append("没有反编译器：结果停留在反汇编与结构层面，不能给出伪代码。")
    if not ({"r2", "rizin"} & have):
        notes.append("没有 radare2/rizin：函数边界与交叉引用由 objdump 输出和字节扫描近似得出。")
    if not ({"pwn", "pyelftools", "lief"} & have):
        notes.append("没有 ELF 解析库：结构信息由内置解析器提供，覆盖段表/符号/动态段，不含 DWARF。")
    if "docker" not in have:
        notes.append(
            "没有 Docker：ELF 程序无法通过容器运行；本机有 WSL 时用 "
            "`wsl -e bash -lc` 运行，否则只能静态分析或打远程服务。"
        )
    return {
        "providers": providers,
        "missing": missing,
        "notes": notes,
        "evidence_id": "re-capabilities",
        "coverage": {"status": "complete", "scope": "本机工具链"},
    }


# --------------------------------------------------------------------------- sniff
def sniff(data: bytes, path: Path | None = None) -> str:
    if data[:4] == b"\x7fELF":
        return "elf"
    if data[:2] == b"MZ":
        return "pe"
    if data[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
                    b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        # 0xcafebabe is shared with Java class files; Java's next four bytes are
        # minor/major version (major >= 45), a fat Mach-O has a small arch count.
        if data[:4] in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
            major = struct.unpack(">H", data[6:8])[0] if len(data) >= 8 else 0
            return "java" if 45 <= major < 200 else "macho"
        return "macho"
    if data[:4] == b"\x00asm":
        return "wasm"
    if data[:4] == b"dex\n":
        return "dex"
    if data[:4] == b"PK\x03\x04":
        if path is not None:
            try:
                with zipfile.ZipFile(path) as zf:
                    names = set(zf.namelist())
                if "AndroidManifest.xml" in names or "classes.dex" in names:
                    return "apk"
                if any(n.endswith(".class") for n in names):
                    return "jar"
            except (OSError, zipfile.BadZipFile):
                pass
        return "zip"
    if data[:2] == b"#!":
        return "script"
    if len(data) > 4 and data[2:4] == b"\r\n":
        return "pyc"
    return "unknown"


def read_bytes(path: Path, limit: int = MAX_READ) -> tuple[bytes, bool]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        data = fh.read(limit)
    return data, size > limit


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- ELF
def _cstr(blob: bytes, off: int) -> str:
    if off < 0 or off >= len(blob):
        return ""
    end = blob.find(b"\x00", off)
    if end < 0:
        end = len(blob)
    return blob[off:end].decode("utf-8", "replace")


def _sec_flags(flags: int) -> str:
    return "".join(c for c, bit in (("W", SHF_WRITE), ("A", SHF_ALLOC), ("X", SHF_EXECINSTR)) if flags & bit) or "-"


def _seg_flags(flags: int) -> str:
    return "".join(c for c, bit in (("R", PF_R), ("W", PF_W), ("X", PF_X)) if flags & bit) or "-"


def parse_elf(data: bytes) -> dict[str, Any]:
    if len(data) < 52 or data[:4] != b"\x7fELF":
        raise ValueError("不是 ELF 文件")
    is64 = data[4] == 2
    end = "<" if data[5] == 1 else ">"
    fmt = end + ("16sHHIQQQIHHHHHH" if is64 else "16sHHIIIIIHHHHHH")
    (_, e_type, e_machine, _e_version, e_entry, e_phoff, e_shoff,
     e_flags, _ehsize, e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from(fmt, data, 0)

    shfmt = end + ("IIQQQQIIQQ" if is64 else "IIIIIIIIII")
    phfmt = end + ("IIQQQQQQ" if is64 else "IIIIIIII")

    def shdr(index: int) -> tuple[int, ...]:
        return struct.unpack_from(shfmt, data, e_shoff + index * e_shentsize)

    if e_shnum == 0 and e_shoff and e_shoff + e_shentsize <= len(data):
        e_shnum = shdr(0)[5]  # sh_size of the null section
    if e_shstrndx == 0xFFFF and e_shoff and e_shoff + e_shentsize <= len(data):
        e_shstrndx = shdr(0)[6]  # sh_link of the null section

    shdrs: list[tuple[int, ...]] = []
    for i in range(min(e_shnum, 4096)):
        off = e_shoff + i * e_shentsize
        if off + e_shentsize > len(data):
            break
        shdrs.append(shdr(i))

    shstr = b""
    if 0 <= e_shstrndx < len(shdrs):
        s = shdrs[e_shstrndx]
        shstr = data[s[4]:s[4] + s[5]]

    sections = []
    for s in shdrs:
        name = _cstr(shstr, s[0])
        sections.append({
            "index": len(sections),
            "name": name,
            "type": SHT.get(s[1], str(s[1])),
            "flags": _sec_flags(s[2]),
            "addr": s[3],
            "offset": s[4],
            "size": s[5],
            "entsize": s[9],
        })

    phdrs: list[tuple[int, ...]] = []
    for i in range(min(e_phnum, 1024)):
        off = e_phoff + i * e_phentsize
        if off + e_phentsize > len(data):
            break
        phdrs.append(struct.unpack_from(phfmt, data, off))

    segments = []
    for p in phdrs:
        if is64:
            p_type, p_flags, p_off, p_vaddr, _paddr, p_filesz, p_memsz, _align = p
        else:
            p_type, p_off, p_vaddr, _paddr, p_filesz, p_memsz, p_flags, _align = p
        segments.append({
            "type": PT.get(p_type, str(p_type)),
            "flags": _seg_flags(p_flags),
            "vaddr": p_vaddr,
            "offset": p_off,
            "filesz": p_filesz,
            "memsz": p_memsz,
        })

    def vaddr_to_off(vaddr: int) -> int | None:
        for p in phdrs:
            if is64:
                p_type, _pf, p_off, p_vaddr, _pa, p_filesz, _pm, _al = p
            else:
                p_type, p_off, p_vaddr, _pa, p_filesz, _pm, _pf, _al = p
            if p_type == PT_LOAD and p_vaddr <= vaddr < p_vaddr + max(p_filesz, 1):
                return p_off + (vaddr - p_vaddr)
        return None

    # ---- dynamic section: deps, soname, rpath, flags
    needed: list[str] = []
    soname = rpath = runpath = ""
    bind_now = False
    flags_1 = 0
    dyn = next((s for s in sections if s["type"] == "DYNAMIC"), None)
    if dyn is not None:
        strtab_off = None
        entries: list[tuple[int, int]] = []
        step = 16 if is64 else 8
        dfmt = end + ("qQ" if is64 else "iI")
        for i in range(min(dyn["size"] // step, 4096)):
            tag, val = struct.unpack_from(dfmt, data, dyn["offset"] + i * step)
            if tag == 0:
                break
            entries.append((tag, val))
        strtab_vaddr = next((v for t, v in entries if t == DT_STRTAB), None)
        if strtab_vaddr is not None:
            strtab_off = vaddr_to_off(strtab_vaddr)
        for tag, val in entries:
            if tag == DT_NEEDED and strtab_off is not None:
                needed.append(_cstr(data, strtab_off + val))
            elif tag == DT_SONAME and strtab_off is not None:
                soname = _cstr(data, strtab_off + val)
            elif tag == DT_RPATH and strtab_off is not None:
                rpath = _cstr(data, strtab_off + val)
            elif tag == DT_RUNPATH and strtab_off is not None:
                runpath = _cstr(data, strtab_off + val)
            elif tag == DT_BIND_NOW:
                bind_now = True
            elif tag == DT_FLAGS_1:
                flags_1 = val
        bind_now = bind_now or bool(flags_1 & DF_1_NOW)

    # ---- symbols
    def read_symbols(section: dict[str, Any]) -> list[dict[str, Any]]:
        if section["index"] >= len(shdrs):
            return []
        s = shdrs[section["index"]]
        link = s[6]
        strtab = b""
        if 0 <= link < len(shdrs):
            ls = shdrs[link]
            strtab = data[ls[4]:ls[4] + ls[5]]
        entsize = s[9] or (24 if is64 else 16)
        fmt_sym = end + ("IBBHQQ" if is64 else "IIIBBH")
        out: list[dict[str, Any]] = []
        count = min(s[5] // entsize if entsize else 0, 20000)
        for i in range(count):
            off = s[4] + i * entsize
            if off + entsize > len(data):
                break
            if is64:
                st_name, st_info, st_other, st_shndx, st_value, st_size = struct.unpack_from(fmt_sym, data, off)
            else:
                st_name, st_value, st_size, st_info, st_other, st_shndx = struct.unpack_from(fmt_sym, data, off)
            name = _cstr(strtab, st_name)
            if not name:
                continue
            out.append({
                "name": name,
                "value": st_value,
                "size": st_size,
                "bind": ("LOCAL", "GLOBAL", "WEAK")[st_info >> 4] if (st_info >> 4) < 3 else str(st_info >> 4),
                "type": ("NOTYPE", "OBJECT", "FUNC", "SECTION", "FILE")[st_info & 0xF]
                if (st_info & 0xF) < 5 else str(st_info & 0xF),
                "shndx": st_shndx,
            })
        return out

    dynamic_symbols: list[dict[str, Any]] = []
    static_symbols: list[dict[str, Any]] = []
    for s in sections:
        if s["type"] == "DYNSYM":
            dynamic_symbols = read_symbols(s)
        elif s["type"] == "SYMTAB":
            static_symbols = read_symbols(s)

    # ---- build id
    build_id = ""
    note = next((s for s in sections if s["name"] == ".note.gnu.build-id"), None)
    if note is not None:
        blob = data[note["offset"]:note["offset"] + note["size"]]
        if len(blob) >= 12:
            namesz, descsz = struct.unpack_from(end + "II", blob, 0)
            desc_off = 12 + ((namesz + 3) // 4) * 4
            build_id = blob[desc_off:desc_off + descsz].hex()

    # ---- mitigations
    stack = next((p for p in phdrs if (p[0] if is64 else p[0]) == PT_GNU_STACK), None)
    if stack is None:
        nx: bool | None = None
    else:
        stack_flags = stack[1] if is64 else stack[6]
        nx = not (stack_flags & PF_X)

    has_relro = any((p[0] if is64 else p[0]) == PT_GNU_RELRO for p in phdrs)
    relro = "full" if has_relro and bind_now else ("partial" if has_relro else "none")
    if e_type == 3:
        pie = "PIE" if flags_1 & DF_1_PIE else "DYN（PIE 或共享库）"
    elif e_type == 2:
        pie = "非 PIE（固定基址）"
    else:
        pie = ELF_TYPES.get(e_type, str(e_type))

    dyn_names = {s["name"] for s in dynamic_symbols}
    canary = "__stack_chk_fail" in dyn_names or "__stack_chk_guard" in dyn_names
    fortify = sorted(n for n in dyn_names if n.endswith("_chk") or n == "__fortify_fail")
    interp = next((_cstr(data, p[3] if not is64 else p[3]) for p in phdrs
                   if (p[0] if is64 else p[0]) == PT_INTERP), "")

    return {
        "format": "elf",
        "class": "ELF64" if is64 else "ELF32",
        "endian": "little" if end == "<" else "big",
        "type": ELF_TYPES.get(e_type, str(e_type)),
        "type_code": e_type,
        "machine": ELF_MACHINES.get(e_machine, f"machine#{e_machine}"),
        "entry": e_entry,
        "flags": e_flags,
        "interpreter": interp,
        "soname": soname,
        "needed": needed,
        "rpath": rpath,
        "runpath": runpath,
        "build_id": build_id,
        "stripped": not static_symbols,
        "segments": segments,
        "sections": sections,
        "symbols": {"dynamic": dynamic_symbols, "static": static_symbols},
        "mitigations": {
            "nx": nx,
            "pie": pie,
            "relro": relro,
            "canary": canary,
            "fortify": fortify,
        },
        "section_index": {s["name"]: s for s in sections if s["name"]},
    }


# --------------------------------------------------------------------------- PE
def parse_pe(data: bytes) -> dict[str, Any]:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("不是 PE 文件")
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_off:pe_off + 4] != b"PE\x00\x00":
        raise ValueError("MZ 头后面没有 PE 签名")
    machine, nsec, _ts, _psym, _nsym, optsz, chars = struct.unpack_from("<HHIIIHH", data, pe_off + 4)
    opt = pe_off + 24
    magic = struct.unpack_from("<H", data, opt)[0] if opt + 2 <= len(data) else 0
    is64 = magic == 0x20B
    entry = struct.unpack_from("<I", data, opt + 16)[0] if opt + 20 <= len(data) else 0
    image_base = struct.unpack_from("<Q" if is64 else "<I", data, opt + (24 if is64 else 28))[0]
    dllchars = struct.unpack_from("<H", data, opt + 70)[0] if opt + 72 <= len(data) else 0
    subsystem = struct.unpack_from("<H", data, opt + 68)[0] if opt + 70 <= len(data) else 0

    sections = []
    sec_off = opt + optsz
    for i in range(min(nsec, 96)):
        off = sec_off + i * 40
        if off + 40 > len(data):
            break
        name, vsize, vaddr, rawsize, rawptr, _pr, _pl, _nr, _nl, schars = struct.unpack_from("<8sIIIIIIHHI", data, off)
        sections.append({
            "index": i,
            "name": name.rstrip(b"\x00").decode("ascii", "replace"),
            "addr": vaddr,
            "offset": rawptr,
            "size": rawsize,
            "vsize": vsize,
            "flags": "X" if schars & 0x20000000 else ("W" if schars & 0x80000000 else "-"),
        })
    return {
        "format": "pe",
        "class": "PE32+" if is64 else "PE32",
        "machine": PE_MACHINES.get(machine, f"machine#0x{machine:x}"),
        "entry": image_base + entry,
        "image_base": image_base,
        "subsystem": subsystem,
        "characteristics": chars,
        "sections": sections,
        "segments": [],
        "symbols": {"dynamic": [], "static": []},
        "mitigations": {
            "nx": bool(dllchars & 0x0100),
            "aslr": bool(dllchars & 0x0040),
            "high_entropy_va": bool(dllchars & 0x0020),
            "cfg": bool(dllchars & 0x4000),
            "pie": "ASLR 可用" if dllchars & 0x0040 else "固定基址",
        },
        "section_index": {s["name"]: s for s in sections if s["name"]},
        "stripped": False,
        "needed": [],
        "interpreter": "",
        "build_id": "",
    }


# --------------------------------------------------------------------------- Mach-O
def parse_macho(data: bytes) -> dict[str, Any]:
    magic_be = struct.unpack_from(">I", data, 0)[0]
    if magic_be in (0xFEEDFACE, 0xFEEDFACF):
        end, is64 = ">", magic_be == 0xFEEDFACF
    elif magic_be in (0xCEFAEDFE, 0xCFFAEDFE):
        end, is64 = "<", magic_be == 0xCFFAEDFE
    else:
        raise ValueError("不是 Mach-O 文件")
    cputype, filetype, ncmds, sizeofcmds, _flags = struct.unpack_from(end + "IIIII", data, 4)
    return {
        "format": "macho",
        "class": "Mach-O 64" if is64 else "Mach-O 32",
        "machine": MACHO_CPUS.get(cputype, f"cpu#0x{cputype:x}"),
        "filetype": filetype,
        "ncmds": ncmds,
        "sizeofcmds": sizeofcmds,
        "sections": [],
        "segments": [],
        "symbols": {"dynamic": [], "static": []},
        "mitigations": {},
        "section_index": {},
        "stripped": False,
        "needed": [],
        "interpreter": "",
        "build_id": "",
        "entry": 0,
    }


def parse_binary(data: bytes, kind: str) -> dict[str, Any]:
    if kind == "elf":
        return parse_elf(data)
    if kind == "pe":
        return parse_pe(data)
    if kind == "macho":
        return parse_macho(data)
    raise ValueError(f"内置解析器不支持 {kind}；用 file/strings 或对应平台的工具")


# --------------------------------------------------------------------------- scans
def scan_ascii(data: bytes, min_length: int = 4) -> list[tuple[int, str]]:
    """Printable runs, with file offsets. Deterministic and dependency-free."""
    out: list[tuple[int, str]] = []
    start = -1
    for i, b in enumerate(data):
        printable = 0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D)
        if printable:
            if start < 0:
                start = i
        elif start >= 0:
            if i - start >= min_length:
                out.append((start, data[start:i].decode("ascii", "replace").rstrip("\r\n")))
            start = -1
    if start >= 0 and len(data) - start >= min_length:
        out.append((start, data[start:].decode("ascii", "replace").rstrip("\r\n")))
    return out


def scan_utf16le(data: bytes, min_length: int = 4) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    i = 0
    start = -1
    chars: list[str] = []
    while i + 1 < len(data):
        ch, nul = data[i], data[i + 1]
        if nul == 0 and 0x20 <= ch < 0x7F:
            if start < 0:
                start = i
            chars.append(chr(ch))
        else:
            if start >= 0 and len(chars) >= min_length:
                out.append((start, "".join(chars)))
            start, chars = -1, []
        i += 2
    if start >= 0 and len(chars) >= min_length:
        out.append((start, "".join(chars)))
    return out


def find_all(haystack: bytes, needle: bytes, limit: int = 5000) -> list[int]:
    out: list[int] = []
    if not needle:
        return out
    pos = haystack.find(needle)
    while pos >= 0 and len(out) < limit:
        out.append(pos)
        pos = haystack.find(needle, pos + 1)
    return out


def offset_to_vaddr(parsed: dict[str, Any], offset: int) -> int | None:
    for seg in parsed.get("segments") or []:
        start = seg.get("offset") or 0
        size = seg.get("filesz") or 0
        if size and start <= offset < start + size:
            return (seg.get("vaddr") or 0) + (offset - start)
    for sec in parsed.get("sections") or []:
        start = sec.get("offset") or 0
        size = sec.get("size") or 0
        if size and start <= offset < start + size:
            return (sec.get("addr") or 0) + (offset - start)
    return None


def vaddr_to_offset(parsed: dict[str, Any], vaddr: int) -> int | None:
    for seg in parsed.get("segments") or []:
        base = seg.get("vaddr") or 0
        size = seg.get("filesz") or 0
        if size and base <= vaddr < base + size:
            return (seg.get("offset") or 0) + (vaddr - base)
    for sec in parsed.get("sections") or []:
        base = sec.get("addr") or 0
        size = sec.get("size") or 0
        if size and base <= vaddr < base + size:
            return (sec.get("offset") or 0) + (vaddr - base)
    return None


def section_at(section_index: dict[str, dict[str, Any]], offset: int) -> str:
    for name, sec in section_index.items():
        start = sec.get("offset") or 0
        size = sec.get("size") or 0
        if size and start <= offset < start + size:
            return name
    return ""


def hexdump(data: bytes, offset: int, length: int = 48) -> str:
    chunk = data[offset:offset + length]
    return " ".join(f"{b:02x}" for b in chunk)


# --------------------------------------------------------------------------- external
def run_tool(argv: list[str], timeout: int = 90) -> tuple[bool, str]:
    """Run an external provider. Returns (ok, output-or-error). Never raises:
    a missing/broken provider is data the caller must report, not an exception."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              errors="replace")
    except FileNotFoundError:
        return False, f"找不到命令：{argv[0]}"
    except subprocess.TimeoutExpired:
        return False, f"{argv[0]} 超时（{timeout}s）"
    except OSError as exc:
        return False, f"{argv[0]} 启动失败：{exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or f"{argv[0]} 返回 {proc.returncode}").strip()[:2000]
    return True, proc.stdout
