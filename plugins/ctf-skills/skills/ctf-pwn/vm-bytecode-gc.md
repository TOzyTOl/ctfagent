# CTF Pwn - Custom Bytecode VM：Tagged Value、移动式 GC 与 Stale 指针类型混淆

适用：题目被编译成自定义字节码/VM（C 壳 + 原生运行时，如 OCaml、JS 引擎），
服务端接收字节码帧并执行；漏洞不在宿主 C 代码的栈上，而在 **VM 的对象模型与 GC**。

相关：本目录 `sandbox-escape.md`（VM 通用逃逸）、`advanced-exploits.md`（VM GC UAF slab reuse）、
`advanced-exploits-2.md`（GC null-reference cascading corruption）。本篇补的是
**“移动式/压缩式 GC + 叶子类型不修正元素指针”** 这一类。

---

## 1. 先摸协议，不要拿文本硬怼

自定义 VM 通常不认文本。用 `gdb -ex "catch syscall read"` 或断在 dispatch 函数上，确认帧格式。
强网杯 babel 的格式是：

```
循环: read_exact(0, &n, 4);            // 小端 u32
      if ((n-1) > 0xffff) break;       // 1..65536
      read_exact(0, buf, n);
      if (babel_dispatch(buf, n) != 0) break;   // 类型错误/坏 opcode 直接断连
```

判据：**先发 4 字节长度再发指令流**；dispatch 返回非 0 连接就断，别把断连误判成崩溃。

同时把 seccomp 一起看了（`main` 里的 BPF 序列）：babel 只放行
`read/write/close/exit/exit_group/openat`，所以终局固定是 `open+read+write`，
不要幻想 `execve`。

## 2. 把值模型和对象布局钉死

从反汇编/运行时把下面几项写成表，后面的利用全靠它：

- **tag 规则**：int 用 `n << 1`（最低位 0）；堆对象 `(off << 4) | kind << 1 | 1`
  ——off 是相对对象堆基址的 16 字节对齐偏移，**对象指针的低位就是 kind**。
- **对象头**：每个 kind 的长度/容量/数据指针在哪。尤其注意同一语义有 **两个长度字段**
  （如 string `+0x10 = len`，`+0x14 = len+1`）时，哪条指令读的是哪一个。
- **GC 根**：数据栈、locals、FFI 寄存器堆。
- **GC 遍历规则**：标记-压缩时**只对 array（kind1/2）递归修正元素指针，其他 kind 当叶子**。
  这一条是整类漏洞的核心：只要把一个容器对象的 kind 改成叶子，它里面的旧指针就不会被修。

## 3. 找“边界检查字段错位”的 1 字节越界

典型模式：下标读写指令用 `+0x14` 当长度，而该字段在**空串或 concat 结果**上是 `len+1`。

```
push string(len=0)     ; +0x14 = 1
push int(4)
op_sset(idx=0)         ; 用 1 做检查，允许 idx=0；写入点 = 对象末尾
```

空串的数据区（inline）紧贴对象头，所以“对象末尾”正好是**下一个堆对象的 kind 低字节**。
最小验证：把下一个 array 的 kind 从 1 改成 4，然后触发 GC 看行为是否变化。

## 4. 用压缩式 GC 把“越界改 kind”升级成 stale 指针

1. 按顺序分配：`D = array(1)`（之后置为 dead）、`R = fnref`、`E = 空串`、`B = array(1)`；
   `B[0] = D`，再用上面的越界字节把 `B.kind` 改成 4（叶子）。
2. 触发 GC：`D` 被回收，`R` 压缩到低地址；**`B` 被当叶子，`B[0]` 仍保存旧值**。
3. 之后 `B[0]` 是一个 stale 值：它指向的对象位置已经被别的对象占用 —— 用 array 的
   下标读指令去读它，就得到“旧对象位置上的新对象”的内容（babel 用它读出
   fnref 的 `+0x18 = &native_table[idx]`，直接泄漏 PIE）。

要点：**压缩会让“旧值”和“新对象”错位**，而叶子 kind 保证错位不会被 GC 修正。
对齐由 dead 对象和分配顺序控制；不要靠猜，读一次验证一次。

## 5. 伪造 tag 对象 → 有界任意读写窗口

babel 的第二步伪造：

- 放一个字符串 `S_c`，其数据区里写入一个 64 位值 `0x409`
  （`off=0x40, kind=4, tag=1` 的伪对象；`+0x14=-1`、`+0x18=3<8` 正好满足 buffer 绑定指令的检查）；
- 用第二次 GC 让 `B'[0]` 的 stale 值精确指向 `S_c` 的数据区；
- `op10(B',0)` 读出 `S_c` 的 data_off，`op10(...,1)` 读出伪造值；
- `op_bind` 用它把 buffer 表 slot 绑到 `目标页`，得到 **±1MB 窗口**的字节读写
  （babel 取 PIE+0x58000 为窗口基址，表项的第二个字段固定 0x100000）。

有了窗口：先读 `read@got` 泄漏 libc；再读堆基址/计数等结构做自检
（babel 校验 `count == 3` 且 `read@got` 落在 0x7f 段，失败就重试）。

## 6. 控制流：native 函数表 + 可控寄存器栈迁移

VM 的 native 调用一般通过一张函数指针表（`fnref.kind=3` 的对象里存 `&table[idx]`）。
既然已有任意写：

- 把 `table[idx]` 覆盖成 libc 的 `mov rsp, rdx ; ret`；
- 找调用约定里 **rdx 正好指向 VM 数据栈** 的那条路径（babel 的 `op_call` 调 native 时
  `rdx = &stack[0]`）；
- 预先用普通 VM 指令把 ROP 链写进数据栈，再 `call`，即完成栈迁移。

ROP 按 seccomp 白名单来：babel 用 libc `open()`（内部是 `openat` 系统调用）+
`read(3,buf,0x200)` + `write(1,buf,0x200)` + `exit(0)`。flag fd 是 3，因为 main
启动时 `close(3..63)`。

## 7. 踩坑清单

- **`op_bind` 正常路径不可达**：正常对象的 `+0x14` 不可能是 `0xffffffff`，必须先伪造。
  看到“检查永远不成立”的指令，不要跳过——它通常是给你伪造用的入口。
- **GC 只遍历部分 kind**：写完 kind 后一定要确认目标 kind 是否被 GC 递归，
  否则“回收后指针错位”的假设不成立。
- **压缩偏移不要手算**：dead 对象控制对齐，但每个 SDK/编译选项都可能改变布局；
  用读写指令把关键地址读回来验证。
- **栈迁移依赖寄存器**：pivot 选哪条 gadget 取决于 native 调用时哪个寄存器可控；
  先反汇编调用点，别套模板。
- **本地复现要用题目 pin 的 libc**：babel 要求 `2.39-0ubuntu8.9`；主机 20.04 的
  `libm` 缺 `GLIBC_2.38` 符号，直接 `dpkg-deb -x libc6_<版本>_amd64.deb` 解出整套
  `ld/libm/libc`，用 `ld-linux --library-path` 启动。
- **flag 按容器随机生成**：读自己容器里的 `/flag`，换容器要重打。
- **自检失败就重试**：VM/GC 对时序和分配很敏感，exp 里保留 3 次重试，
  每次重新建立连接、重新泄漏。

## 8. 边界

- 依赖 GC 是移动/压缩式的；标记-非移动 GC 没有 stale 错位，这条链不成立。
- 依赖“叶子 kind 不修正元素指针”的实现缺陷，或至少依赖某类对象不被遍历。
- 依赖 native 调用路径暴露可控寄存器（rdx/rsi 等）；调用约定不同则换 pivot。
- 依赖 seccomp 白名单里有 `openat/read/write`；若只允许 `read/write`，需要预先打开 fd
  或改成纯 exfiltration。
- 版本敏感：对象布局、指令编码、GOT/表偏移都随运行时版本变化，跨版本必须重新标定。
