"""Reverse-engineering primitives as an in-process MCP server.

Every call returns an **evidence record** rather than a blob:

    {
      "evidence_id": "re-inspect-1a2b3c4d5e",   # addressable; fetch with re_evidence_get
      "provider":    {"name": "builtin", ...},   # who produced it
      "normalized_result": {...},                # the fact, task-shaped
      "coverage":    {"status": "complete"|"partial"|"failed", "scope": "...", "paging": {...}},
      "limitations": [...],                      # what this provider cannot see
      "residual_unknowns": [...]                 # what the caller must still resolve
    }

The shape matters more than the parsers. A worker that reads a blob of
``objdump`` output has to guess what it is looking at; a worker that reads a
coverage status and a limitation list knows whether the answer is final. It is
also what makes the output bounded: paging is a field in the contract, not an
afterthought, so a 40 MB ``.text`` cannot flood the context.

No tool here executes the target. Dynamic analysis stays in Bash, where the
guard hook still applies.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import re_probe

if TYPE_CHECKING:  # pragma: no cover
    from .orchestrator import Run

TOOL_NAMES = [
    "re_capabilities",
    "re_inventory",
    "re_inspect",
    "re_strings",
    "re_search",
    "re_disassemble",
    "re_symbols",
    "re_read",
    "re_xrefs",
    "re_evidence_get",
]

#: One tool result may not exceed this many characters of JSON. Paging exists so
#: this is a ceiling, not a surprise.
MAX_RESULT_CHARS = 60_000
#: Default/maximum rows for any paged list.
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _clamp(limit: Any, default: int = DEFAULT_LIMIT) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_LIMIT))


def _evidence(
    kind: str,
    provider: str,
    result: Any,
    *,
    status: str = "complete",
    scope: str = "",
    limitations: list[str] | None = None,
    unknowns: list[str] | None = None,
    paging: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blob = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]
    record: dict[str, Any] = {
        "evidence_id": f"re-{kind}-{digest}",
        "provider": {"name": provider},
        "normalized_result": result,
        "coverage": {"status": status, "scope": scope or kind},
        "limitations": limitations or [],
        "residual_unknowns": unknowns or [],
    }
    if paging:
        record["coverage"]["paging"] = paging
    if len(blob) > MAX_RESULT_CHARS:
        record["normalized_result"] = {
            "truncated": True,
            "note": (
                f"结果超过 {MAX_RESULT_CHARS} 字符，已截断。请用 limit/offset 分页，"
                "或缩小 query 范围；不要指望一次调用拿到全部内容。"
            ),
        }
        record["coverage"]["status"] = "partial"
    return record


def _page(rows: list[Any], offset: int, limit: int) -> tuple[list[Any], dict[str, Any]]:
    start = max(0, offset)
    window = rows[start:start + limit]
    return window, {
        "offset": start,
        "limit": limit,
        "returned": len(window),
        "total": len(rows),
        "has_more": start + len(window) < len(rows),
        "next_offset": start + len(window) if start + len(window) < len(rows) else None,
    }


def _failure(name: str, error: str, unknown: str) -> dict[str, Any]:
    """A tool that could not run still owes the caller a coverage status."""
    return _evidence(
        name.removeprefix("re_"), "none", {"error": error},
        status="failed", scope=name,
        limitations=[error],
        unknowns=[unknown],
    )


def _embedded_payload(size: int, sections: list[dict[str, Any]],
                      segments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Notice that the interesting part of the file is not the code the loader maps.

    PyInstaller/Nuitka onefile bundles put a 10 MB archive in a section the ELF
    headers barely describe; a worker that stops after ``re_inspect`` sees a
    10 MB file with a 37 KB of mapped code and concludes there is nothing there.
    """
    mapped_end = max((int(s.get("offset", 0)) + int(s.get("filesz", 0)) for s in segments), default=0)
    described_end = max((int(s.get("offset", 0)) + int(s.get("size", 0)) for s in sections), default=0)
    big = [
        {"name": s.get("name", ""), "offset": int(s.get("offset", 0)),
         "size": int(s.get("size", 0))}
        for s in sections
        if int(s.get("size", 0)) >= 1024 * 1024 and "X" not in str(s.get("flags", ""))
    ]
    overlay = max(0, size - max(mapped_end, described_end))
    if not big and overlay < 1024 * 1024:
        return None
    out: dict[str, Any] = {}
    if big:
        out["large_data_sections"] = big
    if overlay >= 1024 * 1024:
        out["trailing_bytes"] = overlay
    return out


#: Markers are matched as raw bytes so they are found in stripped or section-less
#: binaries too. Each is a strong signature, not a substring guess: a generic
#: ".vmp" or "ASPack" match would fire on unrelated data.
_PACKERS = (
    ("upx", b"UPX!", "UPX"),
    ("upx", b"$Info: This file is packed with the UPX", "UPX"),
    ("pyinstaller", b"MEI\x0c\x0b\x0a\x0b\x0e", "PyInstaller"),
    ("pyinstaller", b"pyi-runtime-tmpdir", "PyInstaller"),
)


def _packer_marker(data: bytes) -> dict[str, Any] | None:
    """Say up front that the code being disassembled is a stub.

    A packed binary is still a valid ELF with a valid entry point, so nothing in
    the headers looks wrong; the only tell is the packer's own signature. Without
    this a worker disassembles 600 KB of decompressor and reports "no logic".
    """
    for key, needle, label in _PACKERS:
        at = data.find(needle)
        if at >= 0:
            return {"name": label, "marker_offset": at,
                    "marker": needle.decode("latin-1")}
    return None


class ReTools:
    """Tools bound to one worker task: a workspace to read from, and a registry
    of evidence produced so far."""

    def __init__(self, run: "Run", task_id: int) -> None:
        self.run = run
        self.task_id = task_id
        self._evidence: dict[str, dict[str, Any]] = {}
        self._disasm_cache: dict[tuple[str, int, int], tuple[str, bool]] = {}

    # ------------------------------------------------------------------ plumbing
    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        handler = getattr(self, name, None)
        if handler is None:
            raise ValueError(f"未知工具：{name}")
        result = handler(**arguments)
        if isinstance(result, dict) and "evidence_id" in result:
            self.remember(result)
        return result

    def remember(self, record: dict[str, Any]) -> None:
        self._evidence[record["evidence_id"]] = record
        # Keep the registry bounded; the oldest record is the least useful.
        while len(self._evidence) > 200:
            self._evidence.pop(next(iter(self._evidence)))

    def _resolve(self, path: str) -> Path:
        """Only the run's workspace is readable. An absolute path outside it is
        refused, not silently reinterpreted — a worker that thinks it read the
        host's /etc/passwd and did not would draw a wrong conclusion."""
        raw = Path(path).expanduser()
        if not raw.is_absolute():
            raw = self.run.workspace.work / raw
        resolved = raw.resolve()
        roots = [self.run.workspace.root.resolve(), self.run.workspace.attachments.resolve()]
        if not any(_is_under(resolved, root) for root in roots):
            raise ValueError(f"拒绝读取工作区之外的路径：{path}")
        if not resolved.exists():
            raise ValueError(f"路径不存在：{path}")
        return resolved

    def _binary(self, path: str) -> tuple[Path, bytes, str, dict[str, Any] | None, list[str]]:
        target = self._resolve(path)
        if target.is_dir():
            raise ValueError(f"{path} 是目录；先用 re_inventory 看目录内容")
        data, clipped = re_probe.read_bytes(target)
        kind = re_probe.sniff(data, target)
        limitations: list[str] = []
        parsed: dict[str, Any] | None = None
        if clipped:
            limitations.append(f"文件大于 {re_probe.MAX_READ // (1024 * 1024)} MB，只读取了前部")
        if kind in ("elf", "pe", "macho"):
            try:
                parsed = re_probe.parse_binary(data, kind)
            except (ValueError, IndexError, Exception) as exc:  # noqa: BLE001
                limitations.append(f"内置解析器失败：{exc}")
        return target, data, kind, parsed, limitations

    def _disasm(self, target: Path, start: int, stop: int) -> tuple[str, bool]:
        key = (str(target), target.stat().st_mtime_ns, start)
        if key in self._disasm_cache:
            return self._disasm_cache[key]
        ok, out = re_probe.run_tool([
            "objdump", "-d", "-z", "-M", "intel",
            f"--start-address=0x{start:x}", f"--stop-address=0x{stop:x}",
            str(target),
        ])
        result = (out, ok)
        self._disasm_cache[key] = result
        return result

    # ------------------------------------------------------------------ tools
    def re_capabilities(self) -> dict[str, Any]:
        report = re_probe.capabilities_report()
        report["limitations"] = report.pop("notes")
        return _evidence(
            "capabilities", "builtin", report,
            scope="本机逆向工具链",
            limitations=report["limitations"],
            unknowns=["缺少的工具是否需要安装，由任务需要决定"],
        )

    def re_inventory(self, path: str = ".", limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """What is actually in the attachments directory: kind, size, hash."""
        # Workers run with cwd=work, so a bare "." is the work dir, not the
        # attachments dir they mean by "everything that came with the challenge".
        root = self.run.workspace.attachments.resolve() if path in ("", ".") else self._resolve(path)
        rows: list[dict[str, Any]] = []
        if root.is_file():
            entries = [root]
        else:
            entries = sorted(
                (p for p in root.rglob("*") if p.is_file()),
                key=lambda p: str(p).lower(),
            )[:5000]
        for item in entries:
            try:
                stat = item.stat()
                head, _ = re_probe.read_bytes(item, limit=4096)
            except OSError:
                continue
            kind = re_probe.sniff(head, item)
            row = {
                "path": str(item.relative_to(root)) if root.is_dir() else item.name,
                "size": stat.st_size,
                "kind": kind,
            }
            if stat.st_size <= 32 * 1024 * 1024:
                try:
                    row["sha256"] = re_probe.sha256_bytes(item.read_bytes())
                except OSError:
                    pass
            rows.append(row)
        page, paging = _page(rows, 0, _clamp(limit))
        kinds = sorted({r["kind"] for r in rows})
        return _evidence(
            "inventory", "builtin", {"entries": page, "kinds": kinds},
            scope=f"目录 {path}",
            paging=paging,
            limitations=["只按文件头识别类型；加壳或加密的载荷会被识别为 unknown"],
            unknowns=[f"未展开的 kind：{k}" for k in kinds if k == "unknown"],
        )

    def re_inspect(self, path: str, view: str = "summary") -> dict[str, Any]:
        """Structure and mitigations, from the built-in parsers."""
        target, data, kind, parsed, limitations = self._binary(path)
        provider = "builtin"
        file_line = ""
        if re_probe._which("file"):
            ok, out = re_probe.run_tool(["file", "-b", str(target)], timeout=20)
            if ok:
                file_line = out.strip()
                provider = "builtin+file"

        if parsed is None:
            result = {
                "path": str(target),
                "kind": kind,
                "size": len(data),
                "sha256": re_probe.sha256_bytes(data),
                "file": file_line,
                "head": data[:512].decode("utf-8", "replace"),
            }
            return _evidence(
                "inspect", provider, result,
                status="partial",
                scope=f"文件类型 {kind}",
                limitations=limitations + [f"内置解析器不支持 {kind}；没有做结构解析"],
                unknowns=["需要对应平台的工具才能继续（如 jadx/binwalk/wasm2wat）"],
            )

        sections = parsed.get("sections") or []
        segments = parsed.get("segments") or []
        dyn = parsed.get("symbols", {}).get("dynamic") or []
        static = parsed.get("symbols", {}).get("static") or []
        base = {
            "path": str(target),
            "kind": kind,
            "size": len(data),
            "sha256": re_probe.sha256_bytes(data),
            "file": file_line,
            "class": parsed.get("class"),
            "machine": parsed.get("machine"),
            "type": parsed.get("type") or parsed.get("format"),
            "entry": parsed.get("entry"),
            "entry_hex": f"0x{parsed.get('entry', 0):x}",
            "stripped": parsed.get("stripped"),
            "mitigations": parsed.get("mitigations"),
            "counts": {"sections": len(sections), "segments": len(segments),
                       "dynamic_symbols": len(dyn), "static_symbols": len(static)},
        }

        if view == "sections":
            base["sections"] = sections
        elif view == "segments":
            base["segments"] = segments
        elif view == "dynamic":
            base["interpreter"] = parsed.get("interpreter")
            base["needed"] = parsed.get("needed")
            base["soname"] = parsed.get("soname")
            base["rpath"] = parsed.get("rpath")
            base["runpath"] = parsed.get("runpath")
            base["build_id"] = parsed.get("build_id")
        elif view == "symbols":
            base["symbols"] = {
                "dynamic": dyn[:DEFAULT_LIMIT],
                "static": static[:DEFAULT_LIMIT],
                "note": "用 re_symbols 分页、按名字过滤",
            }
        else:
            base["sections"] = [
                {"name": s.get("name", ""), "type": s.get("type", ""),
                 "flags": s.get("flags", ""), "size": s.get("size", 0)}
                for s in sections if s.get("name")
            ]
            base["needed"] = parsed.get("needed")
            base["interpreter"] = parsed.get("interpreter")

        unknowns: list[str] = []
        if parsed.get("stripped"):
            unknowns.append("符号表已剥离：函数语义只能从反汇编与调用关系推断")
        if kind == "elf" and not (parsed.get("mitigations") or {}).get("canary"):
            unknowns.append("未检测到 canary 符号；若目标静态链接，此项不可靠")
        if kind == "elf" and not segments:
            limitations.append("段表为空或越界，段信息不可信")
        if kind == "elf" and not sections:
            # Common for packed or hand-built binaries. Saying "complete" over an
            # empty section list would read as "this file has no sections".
            limitations.append("没有节头表：节级信息（含 .rodata/.decoy 之类）不可用，只能用段与字节搜索")
        payload = _embedded_payload(len(data), sections, segments)
        if payload:
            base["embedded_payload"] = payload
            unknowns.append(
                "疑似内嵌载荷：结构表描述的代码不是全部。先按 re_search 定位容器魔数，"
                "再决定解包工具，不要只看可执行部分"
            )
        packer = _packer_marker(data)
        if packer:
            base["packer"] = packer
            unknowns.append(
                f"检测到 {packer['name']} 痕迹：静态反汇编看到的是壳，不是程序逻辑。"
                "先脱壳（upx -d / 专用脱壳脚本），再分析产物"
            )
        return _evidence(
            "inspect", provider, base,
            status="partial" if limitations else "complete",
            scope=f"{kind} 结构与保护机制",
            limitations=limitations + (
                [] if kind == "elf" else ["非 ELF：保护机制字段含义与 ELF 不同"]
            ),
            unknowns=unknowns,
        )

    def re_strings(
        self,
        path: str,
        query: str = "",
        min_length: int = 4,
        encoding: str = "ascii",
        offset: int = 0,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        target, data, kind, parsed, limitations = self._binary(path)
        if encoding == "utf16le":
            rows = re_probe.scan_utf16le(data, max(1, int(min_length)))
        elif encoding == "both":
            rows = re_probe.scan_ascii(data, max(1, int(min_length)))
            rows += re_probe.scan_utf16le(data, max(1, int(min_length)))
            rows.sort(key=lambda item: item[0])
        else:
            rows = re_probe.scan_ascii(data, max(1, int(min_length)))

        section_index = (parsed or {}).get("section_index") or {}
        needle = query.lower()
        records = []
        for file_off, text in rows:
            if needle and needle not in text.lower():
                continue
            records.append({
                "offset": file_off,
                "offset_hex": f"0x{file_off:x}",
                "vaddr": re_probe.offset_to_vaddr(parsed, file_off) if parsed else None,
                "section": re_probe.section_at(section_index, file_off),
                "text": text[:400],
            })
        page, paging = _page(records, int(offset or 0), _clamp(limit))
        return _evidence(
            "strings", "builtin", {"encoding": encoding, "query": query, "strings": page},
            status="partial" if paging["has_more"] else "complete",
            scope=f"{kind} 可打印字符串",
            paging=paging,
            limitations=limitations + ["只覆盖 ASCII/UTF-16LE 可打印序列；压缩或加密的字符串不可见"],
            unknowns=[] if records else ["没有匹配的字符串：换更短的关键词或换编码再试"],
        )

    def re_search(
        self,
        path: str,
        pattern: str,
        mode: str = "ascii",
        offset: int = 0,
        limit: int = DEFAULT_LIMIT,
        context: int = 16,
    ) -> dict[str, Any]:
        target, data, kind, parsed, limitations = self._binary(path)
        if mode == "hex":
            cleaned = re.sub(r"[\s,]", "", pattern)
            if len(cleaned) % 2:
                raise ValueError("hex 模式要求偶数个十六进制字符")
            try:
                needle = bytes.fromhex(cleaned)
            except ValueError as exc:
                raise ValueError(
                    f"不是合法的十六进制：{pattern!r}。搜字符串请用 mode='ascii'"
                ) from exc
        elif mode == "utf16le":
            needle = pattern.encode("utf-16-le")
        else:
            needle = pattern.encode("utf-8")
        if not needle:
            raise ValueError("pattern 不能为空")

        hits = re_probe.find_all(data, needle, limit=20000)
        section_index = (parsed or {}).get("section_index") or {}
        records = []
        for file_off in hits:
            records.append({
                "offset": file_off,
                "offset_hex": f"0x{file_off:x}",
                "vaddr": re_probe.offset_to_vaddr(parsed, file_off) if parsed else None,
                "section": re_probe.section_at(section_index, file_off),
                "context": re_probe.hexdump(data, max(0, file_off - int(context)), int(context) * 2 + len(needle)),
            })
        page, paging = _page(records, int(offset or 0), _clamp(limit))
        if hits:
            unknowns: list[str] = []
        elif mode == "ascii":
            unknowns = ["没有命中；可能被压缩/加密，或字符串是运行时拼出来的。试 mode='hex' 找字节"]
        elif mode == "hex":
            unknowns = ["没有命中；确认字节序与是否被压缩/加密。搜字符串请用 mode='ascii'"]
        else:
            unknowns = ["没有命中；确认宽字符编码（UTF-16LE）与字节序"]
        return _evidence(
            "search", "builtin", {"mode": mode, "pattern": pattern, "matches": page},
            status="partial" if paging["has_more"] else "complete",
            scope=f"{kind} 字节搜索",
            paging=paging,
            limitations=limitations + [
                "纯字节匹配：不识别重定位、加密或运行时构造的常量",
            ],
            unknowns=unknowns,
        )

    def re_disassemble(
        self,
        path: str,
        start: str = "entry",
        count: int = 60,
        stop: str = "",
    ) -> dict[str, Any]:
        target, data, kind, parsed, limitations = self._binary(path)
        if not re_probe._which("objdump"):
            return _evidence(
                "disasm", "none", {"path": str(target)},
                status="failed",
                scope="反汇编",
                limitations=["PATH 里没有 objdump"],
                unknowns=["无法反汇编；可在工作区安装 binutils，或改用 Docker 镜像内的工具"],
            )
        if kind not in ("elf", "pe", "macho"):
            return _evidence(
                "disasm", "objdump", {"path": str(target), "kind": kind},
                status="failed",
                scope="反汇编",
                limitations=[f"objdump 不处理 {kind}"],
                unknowns=[],
            )

        begin = self._address(start, parsed)
        if begin is None:
            raise ValueError(f"无法解析起点：{start}")
        if stop:
            end = self._address(stop, parsed)
            if end is None:
                raise ValueError(f"无法解析终点：{stop}")
        else:
            end = begin + max(1, int(count)) * 16

        out, ok = self._disasm(target, begin, end)
        if not ok:
            return _evidence(
                "disasm", "objdump", {"path": str(target), "start": begin, "error": out},
                status="failed",
                scope=f"0x{begin:x}–0x{end:x}",
                limitations=[out],
                unknowns=["该地址区间不可反汇编：确认起点是否落在代码段内"],
            )
        lines = [line for line in out.splitlines() if re.match(r"\s*[0-9a-f]+:", line)]
        lines = lines[: max(1, min(int(count), 5000))]
        parsed_lines = []
        for line in lines:
            addr_s, _, rest = line.strip().partition(":")
            rest = rest.lstrip("\t")
            parts = rest.split("\t")
            if len(parts) >= 2 and parts[0].strip():
                raw, insn = parts[0].strip(), "\t".join(parts[1:]).strip()
            else:
                # objdump -d (no raw bytes) puts the mnemonic where the bytes
                # would be; splitting on the first run of spaces recovers both
                # for the byte-annotated form and leaves the other intact.
                bits = rest.strip().split(None, 1)
                if len(bits) == 2 and re.fullmatch(r"(?:[0-9a-f]{2}\s*)+", bits[0]):
                    raw, insn = bits[0], bits[1]
                else:
                    raw, insn = "", rest.strip()
            parsed_lines.append({"addr": f"0x{int(addr_s, 16):x}", "bytes": raw, "insn": insn})
        last = int(parsed_lines[-1]["addr"], 16) if parsed_lines else begin
        # Resume exactly after the last instruction we returned. Using last+1
        # would land inside a multi-byte instruction and produce a bogus
        # disassembly on the next page.
        last_len = max(1, len(parsed_lines[-1]["bytes"].split())) if parsed_lines else 1
        resume = last + last_len
        return _evidence(
            "disasm", "objdump", {
                "path": str(target),
                "start": f"0x{begin:x}",
                "end": f"0x{last:x}",
                "instructions": parsed_lines,
            },
            scope=f"0x{begin:x}–0x{last:x}",
            limitations=limitations + [
                "线性反汇编：数据嵌入代码段时会把数据当指令",
                "没有函数边界与调用图；跳转目标需自行解析",
            ],
            paging={"offset": 0, "limit": int(count), "returned": len(parsed_lines),
                    "total": None, "has_more": True, "next_offset": f"0x{resume:x}"},
            unknowns=["反汇编正确不等于语义正确：控制流与数据来源仍需验证"],
        )

    def re_symbols(
        self,
        path: str,
        query: str = "",
        which: str = "all",
        offset: int = 0,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        target, data, kind, parsed, limitations = self._binary(path)
        if parsed is None:
            return _evidence(
                "symbols", "builtin", {"path": str(target), "kind": kind},
                status="failed",
                scope="符号表",
                limitations=limitations + [f"内置解析器不支持 {kind}"],
                unknowns=[],
            )
        pool: list[dict[str, Any]] = []
        if which in ("all", "dynamic"):
            pool += [{**s, "table": "dynamic"} for s in parsed.get("symbols", {}).get("dynamic") or []]
        if which in ("all", "static"):
            pool += [{**s, "table": "static"} for s in parsed.get("symbols", {}).get("static") or []]
        needle = query.lower()
        if needle:
            pool = [s for s in pool if needle in s["name"].lower()]
        page, paging = _page(pool, int(offset or 0), _clamp(limit))
        stripped = parsed.get("stripped")
        return _evidence(
            "symbols", "builtin", {"which": which, "query": query, "symbols": page},
            status="partial" if paging["has_more"] or stripped else "complete",
            scope="符号表",
            paging=paging,
            limitations=limitations + (["文件已剥离：静态符号表为空，只剩动态符号"] if stripped else []),
            unknowns=["剥离后函数名不可得；可尝试从字符串引用与调用顺序推断"],
        )

    def re_read(
        self,
        path: str,
        offset: str = "0",
        length: int = 256,
        address: str = "",
    ) -> dict[str, Any]:
        target, data, kind, parsed, limitations = self._binary(path)
        size = max(1, min(int(length), 65536))
        if address:
            vaddr = self._address(address, parsed)
            if vaddr is None:
                raise ValueError(f"无法解析地址：{address}")
            file_off = re_probe.vaddr_to_offset(parsed, vaddr) if parsed else None
            if file_off is None:
                return _evidence(
                    "read", "builtin", {"path": str(target), "address": address},
                    status="failed",
                    scope=f"地址 0x{vaddr:x}",
                    limitations=limitations + ["该虚拟地址不在任何可映射段内（可能是 .bss 或运行时分配）"],
                    unknowns=["运行时内存需要在调试器里读，静态文件里没有"],
                )
        else:
            file_off = self._address(offset, parsed) if isinstance(offset, str) and not offset.isdigit() else int(offset)
            if file_off is None:
                raise ValueError(f"无法解析偏移：{offset}")
        file_off = max(0, min(file_off, max(0, len(data) - 1)))
        chunk = data[file_off:file_off + size]
        section_index = (parsed or {}).get("section_index") or {}
        return _evidence(
            "read", "builtin", {
                "path": str(target),
                "offset": file_off,
                "offset_hex": f"0x{file_off:x}",
                "vaddr": re_probe.offset_to_vaddr(parsed, file_off) if parsed else None,
                "section": re_probe.section_at(section_index, file_off),
                "length": len(chunk),
                "hex": re_probe.hexdump(chunk, 0, len(chunk)),
                "ascii": "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk),
            },
            scope=f"文件偏移 0x{file_off:x}",
            limitations=limitations,
        )

    def re_xrefs(
        self,
        path: str,
        target_ref: str,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Approximate cross-references: which instructions mention this address
        or immediate. objdump annotates rip-relative operands with the resolved
        target, which is what makes this usable without a real disassembler."""
        target, data, kind, parsed, limitations = self._binary(path)
        if not re_probe._which("objdump"):
            return _evidence(
                "xrefs", "none", {"path": str(target)},
                status="failed", scope="交叉引用",
                limitations=["PATH 里没有 objdump"],
                unknowns=["无法计算交叉引用"],
            )
        wanted = self._address(target_ref, parsed)
        if wanted is not None:
            needle_text = f"0x{wanted:x}"
            # A bare substring test makes 0x10 match 0x1000 and 0x10f. Require
            # the hex digits to end where the token does.
            pattern = re.compile(rf"(?<![0-9a-zA-Z]){re.escape(needle_text)}(?![0-9a-fA-F])")
        else:
            needle_text = target_ref
            pattern = re.compile(rf"(?<![0-9a-zA-Z_]){re.escape(needle_text)}(?![0-9a-zA-Z_])")
        # Distinct key from _disasm: this caches the whole-file dump, which is
        # not the bounded range a re_disassemble(start=0) call would ask for.
        cache_key = (str(target), target.stat().st_mtime_ns, -1)
        if cache_key not in self._disasm_cache:
            ok, out = re_probe.run_tool(["objdump", "-d", "-M", "intel", str(target)], timeout=180)
            self._disasm_cache[cache_key] = (out, ok)
        out, ok = self._disasm_cache[cache_key]
        if not ok:
            return _evidence(
                "xrefs", "objdump", {"path": str(target), "error": out},
                status="failed", scope="交叉引用",
                limitations=[out], unknowns=["反汇编失败，无法计算交叉引用"],
            )
        hits = []
        for line in out.splitlines():
            if pattern.search(line) and re.match(r"\s*[0-9a-f]+:", line):
                addr_s, _, rest = line.strip().partition(":")
                hits.append({"addr": f"0x{int(addr_s, 16):x}", "line": rest.strip()[:300]})
        page, paging = _page(hits, 0, _clamp(limit))
        return _evidence(
            "xrefs", "objdump", {"path": str(target), "target": needle_text, "xrefs": page},
            status="partial",
            scope="整个代码段的文本匹配",
            paging=paging,
            limitations=limitations + [
                "这是文本匹配，不是真正的数据流分析：间接跳转、计算出的地址不会出现",
                "完整反汇编在内存中截断到 64 MB 文本，超大目标可能漏检",
            ],
            unknowns=["交叉引用为空不代表没有引用：可能经过寄存器间接寻址"],
        )

    def re_evidence_get(self, evidence_id: str) -> dict[str, Any]:
        record = self._evidence.get(evidence_id)
        if record is None:
            return {"error": f"没有这条证据：{evidence_id}（只保留本次任务最近 200 条）"}
        return record

    # ------------------------------------------------------------------ helpers
    def _address(self, text: Any, parsed: dict[str, Any] | None) -> int | None:
        if isinstance(text, int):
            return text
        raw = str(text).strip()
        if not raw:
            return None
        if raw.lower() in ("entry", "start"):
            return int((parsed or {}).get("entry") or 0)
        try:
            return int(raw, 0)
        except ValueError:
            pass
        if parsed:
            section = (parsed.get("section_index") or {}).get(raw)
            if section is not None:
                return int(section.get("addr") or 0)
            for table in ("dynamic", "static"):
                for sym in (parsed.get("symbols", {}).get(table) or []):
                    # An undefined import has value 0; treating that as a real
                    # address makes every reference to 0x0 look like a hit.
                    if sym["name"] == raw and int(sym.get("shndx") or 0) != 0:
                        return int(sym["value"])
        # "base+0x10"
        if "+" in raw:
            head, _, tail = raw.partition("+")
            base = self._address(head, parsed)
            try:
                return None if base is None else base + int(tail, 0)
            except ValueError:
                return None
        return None


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "re_capabilities",
            "description": (
                "Report which reverse-engineering providers exist on this host and what each "
                "one adds. Call this before concluding a tool is missing. The result lists "
                "gaps explicitly so you can plan around them."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "re_inventory",
            "description": (
                "List files under a workspace path with kind, size and sha256. First call for "
                "any new challenge: it tells you which attachment is the real target."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative to the working directory, or '.'."},
                    "limit": {"type": "integer", "default": DEFAULT_LIMIT},
                },
                "required": [],
            },
        },
        {
            "name": "re_inspect",
            "description": (
                "Parse ELF/PE/Mach-O structure with the built-in parser: class, machine, entry, "
                "sections, segments, dynamic dependencies and mitigations (NX, PIE, RELRO, "
                "canary, FORTIFY). view=summary|sections|segments|dynamic|symbols."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "view": {
                        "type": "string",
                        "enum": ["summary", "sections", "segments", "dynamic", "symbols"],
                        "default": "summary",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "re_strings",
            "description": (
                "Extract printable strings with file offset, virtual address and section. "
                "Paged: read coverage.paging.next_offset and pass it back as offset."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "query": {"type": "string", "description": "Case-insensitive substring filter."},
                    "min_length": {"type": "integer", "default": 4},
                    "encoding": {"type": "string", "enum": ["ascii", "utf16le", "both"], "default": "ascii"},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": DEFAULT_LIMIT},
                },
                "required": ["path"],
            },
        },
        {
            "name": "re_search",
            "description": (
                "Search raw bytes for an ASCII string (default), a hex pattern or a "
                "UTF-16LE string. Returns file offset, virtual address, section and a "
                "hexdump of the surrounding bytes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "pattern": {"type": "string"},
                    "mode": {"type": "string", "enum": ["ascii", "hex", "utf16le"], "default": "ascii"},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": DEFAULT_LIMIT},
                    "context": {"type": "integer", "default": 16},
                },
                "required": ["path", "pattern"],
            },
        },
        {
            "name": "re_disassemble",
            "description": (
                "Disassemble a bounded address range with objdump (Intel syntax). start accepts "
                "an address, a symbol name, 'entry', or 'base+0x10'. Returns instructions with "
                "raw bytes. Use coverage.paging.next_offset as the next start."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "string", "default": "entry"},
                    "stop": {"type": "string", "default": ""},
                    "count": {"type": "integer", "default": 60},
                },
                "required": ["path"],
            },
        },
        {
            "name": "re_symbols",
            "description": "List dynamic/static symbols, optionally filtered by name, paged.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "query": {"type": "string"},
                    "which": {"type": "string", "enum": ["all", "dynamic", "static"], "default": "all"},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": DEFAULT_LIMIT},
                },
                "required": ["path"],
            },
        },
        {
            "name": "re_read",
            "description": (
                "Read a bounded byte range by file offset or virtual address and return hex + "
                "ASCII. Use it to confirm a string, a header field or a table entry."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "string", "default": "0"},
                    "address": {"type": "string", "default": ""},
                    "length": {"type": "integer", "default": 256},
                },
                "required": ["path"],
            },
        },
        {
            "name": "re_xrefs",
            "description": (
                "Find instructions that mention an address or immediate (approximate cross "
                "references, via objdump text). Works for code that loads a string or a global "
                "by absolute or rip-relative address; does not follow indirect jumps."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "target_ref": {"type": "string", "description": "Address, symbol name, or immediate text."},
                    "limit": {"type": "integer", "default": DEFAULT_LIMIT},
                },
                "required": ["path", "target_ref"],
            },
        },
        {
            "name": "re_evidence_get",
            "description": (
                "Fetch a previously returned evidence record by evidence_id. Use it instead of "
                "re-running a tool whose result you already have."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"evidence_id": {"type": "string"}},
                "required": ["evidence_id"],
            },
        },
    ]


def build_sdk_mcp_server(tools: ReTools) -> Any:
    import asyncio  # noqa: PLC0415

    import claude_agent_sdk as sdk  # noqa: PLC0415

    async def make(name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = tools.call(name, args)
            if asyncio.iscoroutine(result):
                result = await result
        except TypeError as exc:
            result = _failure(name, f"参数不对：{exc}", "调用参数与 schema 不一致")
            tools.remember(result)
        except Exception as exc:  # noqa: BLE001
            # Even a refusal is an evidence record: the caller must see that the
            # scope was not observed, not mistake an error string for "not found".
            result = _failure(name, f"{type(exc).__name__}: {exc}", str(exc))
            tools.remember(result)
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]}

    sdk_tools = []
    for spec in tool_specs():
        name = spec["name"]

        async def handler(args: dict[str, Any], _name: str = name) -> dict[str, Any]:
            return await make(_name, args)

        sdk_tools.append(sdk.tool(name, spec["description"], spec["input_schema"])(handler))
    return sdk.create_sdk_mcp_server(name="re", version="1.0.0", tools=sdk_tools)
