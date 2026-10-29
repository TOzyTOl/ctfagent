# CTF Reverse - Anti-Analysis CTF Writeups

CTF-specific anti-analysis techniques: signal-handler tricks, instruction-trace inversion, call-less function chaining, parent-patched child binary dumping. For the core anti-analysis taxonomy (Linux/Windows anti-debug, anti-VM, anti-DBI, code integrity, anti-disassembly), see [anti-analysis.md](anti-analysis.md).

## Table of Contents
- [SIGILL Handler for Execution Mode Switching (Hack.lu 2015)](#sigill-handler-for-execution-mode-switching-hacklu-2015)
- [SIGFPE Signal Handler Side-Channel via strace Counting (PlaidCTF 2017)](#sigfpe-signal-handler-side-channel-via-strace-counting-plaidctf-2017)
- [Instruction Trace Inversion with Keystone and Unicorn (MeePwn CTF 2017)](#instruction-trace-inversion-with-keystone-and-unicorn-meepwn-ctf-2017)
  - [Call-less Function Chaining via Stack Frame Manipulation (THC CTF 2018)](#call-less-function-chaining-via-stack-frame-manipulation-thc-ctf-2018)
  - [Parent-Patched Child Binary Dump via strace process_vm_writev (Google CTF Quals 2018)](#parent-patched-child-binary-dump-via-strace-process_vm_writev-google-ctf-quals-2018)
  - [In-Process Detour: the Static `call` Points at Dead Code](#in-process-detour-the-static-call-points-at-dead-code)
- [ConfuserEx Dynamic Module Dump via Constructor Breakpoint (Kaspersky 2018)](#confuserex-dynamic-module-dump-via-constructor-breakpoint-kaspersky-2018)

---

## SIGILL Handler for Execution Mode Switching (Hack.lu 2015)

Binaries may install SIGILL (illegal instruction) handlers to switch between x86 and x86-64 execution modes or implement custom opcode dispatch:

1. **Signal registration:** `signal(SIGILL, handler)` installs a callback for illegal instruction exceptions
2. **Mode switching:** The handler modifies the saved instruction pointer or segment registers to switch between 32-bit and 64-bit code
3. **Custom opcodes:** Invalid x86 instructions trigger the handler, which interprets operand bytes as custom VM opcodes

```c
// Signal handler decodes "illegal" instructions as custom opcodes
void sigill_handler(int sig, siginfo_t *info, void *ucontext) {
    ucontext_t *ctx = (ucontext_t *)ucontext;
    unsigned char *pc = (unsigned char *)ctx->uc_mcontext.gregs[REG_RIP];
    // Decode custom opcode from bytes at PC
    // Advance PC past the custom instruction
    ctx->uc_mcontext.gregs[REG_RIP] += opcode_length;
}
```

**Key insight:** If a binary installs signal handlers for SIGILL/SIGSEGV/SIGTRAP early in execution, suspect custom instruction dispatch. Trace signal deliveries with `strace -e signal` or set GDB to not intercept: `handle SIGILL nostop pass`.

---

## SIGFPE Signal Handler Side-Channel via strace Counting (PlaidCTF 2017)

Binary uses SIGFPE signal handlers for control flow, making static analysis unreliable. Brute-force by counting SIGFPE signals via strace — correct input characters produce more signals.

```bash
# Count SIGFPE signals per input character guess
for c in {a..z} {A..Z} {0..9}; do
    count=$(echo -n "${c}AAAAAAA" | strace -e signal=SIGFPE ./binary 2>&1 | grep -c SIGFPE)
    echo "$c: $count"
done
# Character producing the most SIGFPEs is correct
# Repeat for each position, extending the known prefix
```

**Key insight:** Signal handlers (SIGFPE, SIGSEGV, SIGILL) create implicit control flow invisible to static analysis. The number of signals raised correlates with validation progress. Counting signals via `strace -e signal=SIGFPE` turns opaque signal-based validation into a measurable side-channel for character-by-character brute-force.

---

## Instruction Trace Inversion with Keystone and Unicorn (MeePwn CTF 2017)

UPX-packed binary applies a sequence of arithmetic-only transforms (sub, add, xor, rol, ror) to the flag. No memory side-effects — purely register arithmetic. IDAPython traces non-jump instructions, the sequence is then inverted to recover the flag.

**Inversion rules:**
- Reverse the instruction sequence (last instruction first)
- Swap inverse pairs: `add ↔ sub`, `rol ↔ ror`, `xor` is self-inverse

```python
# IDAPython: collect non-jump instructions in the obfuscated routine
import idaapi, idc

def trace_transforms(start_ea, end_ea):
    instructions = []
    ea = start_ea
    while ea < end_ea:
        mnem = idc.print_insn_mnem(ea)
        if mnem not in ('jmp', 'je', 'jne', 'call', 'ret'):
            instructions.append((ea, mnem, idc.print_operands(ea)))
        ea = idc.next_head(ea)
    return instructions

transforms = trace_transforms(0x401000, 0x401200)

# Invert: reverse order, swap add/sub and rol/ror
inverse_map = {'add': 'sub', 'sub': 'add', 'rol': 'ror', 'ror': 'rol', 'xor': 'xor'}
inverted = [(mnem, op) for (_, mnem, op) in reversed(transforms)]
inverted = [(inverse_map.get(m, m), op) for m, op in inverted]
```

```python
# Assemble inverted instructions with Keystone, emulate with Unicorn
from keystone import *
from unicorn import *
from unicorn.x86_const import *

ks = Ks(KS_ARCH_X86, KS_MODE_64)
uc = Uc(UC_ARCH_X86, UC_MODE_64)

asm_src = '\n'.join(f'{mnem} {op}' for mnem, op in inverted)
encoding, _ = ks.asm(asm_src)

CODE_BASE = 0x400000
uc.mem_map(CODE_BASE, 0x10000)
uc.mem_write(CODE_BASE, bytes(encoding))

# Set initial register state to the observed output value
uc.reg_write(UC_X86_REG_RAX, known_output)
uc.emu_start(CODE_BASE, CODE_BASE + len(encoding))
flag_bytes = uc.reg_read(UC_X86_REG_RAX).to_bytes(8, 'little')
```

**PEB anti-debug note:** If the binary reads `PEB.BeingDebugged` and uses it to select between two comparison target values, the traced instructions under IDAPython may use the debug-mode target. Patch `BeingDebugged` to 0 before tracing, or identify both branches and use the non-debug target value.

**Key insight:** Arithmetic-only obfuscation (no memory writes) is fully reversible by tracing, inverting the instruction sequence, and swapping inverse operations. PEB anti-debug can silently change comparison targets — always verify which branch is taken.

**References:** MeePwn CTF 2017

---

### Call-less Function Chaining via Stack Frame Manipulation (THC CTF 2018)

**Pattern:** Binary hides function calls by building a linked list of function pointers on the stack, then modifying saved RBP and return addresses so `leave; ret` instructions chain through the list without any explicit `CALL` instructions. IDA fails to decompile because push/pop are unbalanced and function boundaries cannot be determined.

Each function in the chain:
1. Pushes operands and the next function's address onto the stack
2. Sets saved RBP to point to the next stack frame
3. Sets the return address to the next function
4. `leave` restores RSP from RBP (moving to next frame), `ret` jumps to the next function

```python
# Reversed processing chain (each function applied via leave/ret):
def reverse_processing(byte):
    res = byte | 0x80       # OR 0x80
    res = res ^ 0xCA        # XOR 0xCA
    res = (res + 66) & 0xFF # ADD 66
    res = res ^ 0xCA        # XOR 0xCA (repeated)
    res = (res + 66) & 0xFF
    res = res ^ 0xCA
    res = (res + 66) & 0xFF
    res = res ^ 0xFE        # XOR 0xFE (final)
    return res
# Apply in reverse order, then reverse the character sequence
```

**Key insight:** By manipulating saved RBP to point to the next stack frame and saved RIP to the next function, `leave; ret` chains through functions without any `call` instructions. Disassemblers that track call/ret balance fail to identify function boundaries. Patch each function body individually for IDA to handle them.

**Detection:** Binary with many small code blocks ending in `leave; ret` but no corresponding `call` instructions. Stack contains interleaved function pointers and data. IDA shows "stack frame is too big" or fails to create functions.

**References:** THC CTF 2018

---

### Parent-Patched Child Binary Dump via strace process_vm_writev (Google CTF Quals 2018)

**Pattern (Keygenme):** The binary forks. The child is stub code full of `int3` (`0xcc`) traps. The parent uses `ptrace` + `process_vm_writev` to write the real instructions into the child right before each trap fires, then stepping continues. Static analysis of the child sees only junk; dynamic analysis in a single-process debugger misses the parent's writes.

**Bypass — let strace do the work:**
```bash
# Record every process_vm_writev the parent performs, including full iov contents.
strace -f -e trace=process_vm_writev -e write=all -o trace.log ./keygenme

# Each entry looks like:
#   process_vm_writev(child_pid, [{iov_base="\x48\x89\xe5...", iov_len=12}], 1,
#                     [{iov_base=0x400c80, iov_len=12}], 1, 0) = 12
```

Parse the log to extract `(remote_addr, bytes)` pairs and emit an IDA `patch_bytes` script:
```python
import re, pathlib
patches = []
pattern = re.compile(
    r'process_vm_writev\(\d+, \[{iov_base="([^"]+)", iov_len=(\d+)}\].*?\[{iov_base=(0x[0-9a-f]+)',
)
for m in pattern.finditer(pathlib.Path('trace.log').read_text()):
    data = m.group(1).encode('latin1').decode('unicode_escape').encode('latin1')
    addr = int(m.group(3), 16)
    patches.append((addr, data))

with open('patch.py', 'w') as fh:
    for addr, data in patches:
        for i, b in enumerate(data):
            fh.write(f'patch_byte({addr + i:#x}, {b:#x})\n')
```

Load `patch.py` in IDA (File → Script file) to apply every parent-written instruction, turning the trap-riddled child into a fully readable binary. With the patched binary, the crypto routine is a plain loop — black-box the irreversible portion and replace the final `strcmp` with a leak of the expected value.

**Key insight:** Any anti-analysis scheme that uses a ptracer to rewrite the tracee's text is transparent to `strace` on the parent. `process_vm_writev` calls carry both the target address and the bytes, so a one-pass strace run is enough to dump the real code. The same trick works for self-modifying packers that use `ptrace(PTRACE_POKEDATA)` or `write()` into `/proc/<pid>/mem`.

**References:** Google CTF Quals 2018 — writeup 10330

---

### In-Process Detour: the Static `call` Points at Dead Code

**Pattern (easyrs, Windows PE32+, Rust):** the disassembly is honest about *which*
functions exist and dishonest about *which one runs*. `main` contains

```asm
; static listing — what every disassembler shows
0x140003c5a  call  0x140004c40        ; rc4(key, data, len)
```

but at `0x14000383b` — before that call site is ever reached — the program hands
its own address to a patching library:

```asm
0x14000381e  lea   rdx, [0x1400037e0]     ; target: a 6-byte stub
0x140003825  lea   rcx, [0x140003c61]     ; hijack point: inside the call above
0x14000383b  call  <retour::detour>       ; or plthook / mprotect + memcpy
...
0x1400037e0  mov   eax, 7                 ; the "GetTickCount" everyone imports
0x1400037e5  ret
```

At runtime the five bytes at `0x140003c61` are rewritten to reach a *different*
callee, and the function the listing shows (`0x140004c40`, an RC4 in this sample)
never executes. The key that actually got used was not the RC4 key; the transform
was XOR → byte-reverse → PKCS#7 → **AES-128-ECB**.

**The dependency list is the tell.** You do not need to read a single byte of the
disassembly to smell this:

| Imported crate / lib | What it is for |
|---|---|
| `retour`, `detour`, `minhook`, `frida-gum` | inline hooks that rewrite a call site |
| `plthook`, `libudis86`, `iced-x86`, `capstone` | decode + relocate the prologue being hooked |
| `region`, `VirtualProtect`, `mprotect` | flip the text page writable |
| `mmap_fixed` (`MAP_FIXED`), `memmap2` | put code at an address the hook expects |

Any of these in the imports means **every `call` in the static listing is a guess
until you have watched it move.** A binary that hooks `GetTickCount`, `IsDebuggerPresent`
or its own crypto wrapper is not trying to stop you; it is telling you the constants
you read are inputs, not facts.

**Settle it with a known-answer control, not more disassembly.** This is the whole
method, and it costs a minute:

```python
# The model says: check == AES-ECB(key, pkcs7(reverse(xor(data))))
# Then a value the model predicts is *accepted* must be accepted.
predicted = aes_ecb(key_from_static_model, pkcs7(reverse_bytes(xor_bytes(b"A" * 16))))
# Feed it in and watch which branch prints what.
#   "Success" -> model confirmed, go build the solver
#   "Fail"    -> model wrong, do NOT derive anything further from it
```

Observed on the easyrs run (2026-10-27): the worker *found* the detour — it
reasoned about `0x14000383b` passing `0x1400037e0` to `plthook` — then wrote
"RC4 with a 16-byte key" to its blackboard as `inferred`, spent its remaining
budget deriving that key, and never ran the one input that would have refuted it
on the spot.

**Detection checklist:**
- `call` to a 5–6 byte function ending in `ret`, whose body returns a constant
  (`mov eax, N; ret`) — a stub that is only ever a *hook target*, never a real
  function.
- `lea rcx, [site_inside_a_far_function]` paired with `lea rdx, [stub]`.
- A seed/clock/time import that is never called from anywhere else.
- A crypto-named import (`rc4`, `aes`) that is present but whose call sites sit
  in functions with no xrefs from `main`.

**Bypass recipes, cheapest first:**
1. **Known-answer control** (above) — no debugger, no dump, settles the model.
2. **Read the bytes back**: run under a debugger, `x/5bx 0x140003c61` at the first
   breakpoint after `main`'s init, compare to the file. Different bytes = detoured.
3. **Dump the text section** after init (`ReadProcessMemory` / a minidump /
   Scylla-style dump) and disassemble *that* — the dumped image is post-patch.
4. **Force the stub's real behaviour** when the hook only fakes a seed: patch the
   stub to return the value you want, re-run, and see whether the check responds
   (proves the value is an input) or not (proves it is dead code).

**Key insight:** A static call graph is a claim about *reachability*, and a
program that patches itself can falsify that claim without leaving a trace in the
file. The imports tell you it plans to; a known-answer run tells you what it
actually did. When the static model and the observed output disagree, the static
model is wrong — reproducing the observed output is the only proof.

**References:** easyrs sample (2026-10-27 bench run) — `retour` 0.3.1 + `plthook`
+ `libudis86` + `region` + `mmap_fixed_fixed`; answer was
`96b30dd3d5f0ec32ecdf3147fe8d39ce`.

---

## ConfuserEx Dynamic Module Dump via Constructor Breakpoint (Kaspersky 2018)

**Pattern:** ConfuserEx (.NET protector) encrypts method bodies and decrypts them at runtime from a `<Module>` constructor. Break on the constructor in dnSpy, step until the dynamic module is fully built in memory, right-click → **Save Module** to dump the decrypted assembly with tokens intact. Run `de4dot` over the dump to rename obfuscated symbols.

```text
dnSpy:
  File → Open → target.exe
  Assembly Explorer → <Module> .cctor → F9 (breakpoint)
  F5 to run; wait until loaded
  Right-click assembly → Save Module → out.exe
$ de4dot out.exe        # symbol cleanup
```

**Key insight:** ConfuserEx protects on-disk code but not the runtime representation. Any time a .NET protector ships a compiled constructor that performs decryption, the dumped post-constructor module is the cleartext binary. Chain with de4dot to undo the follow-up symbol obfuscation.

**References:** Kaspersky Industrial CTF 2018 — glardomos, writeup 12325
