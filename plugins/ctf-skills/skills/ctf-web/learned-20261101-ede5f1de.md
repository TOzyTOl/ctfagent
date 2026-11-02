<!-- 沉淀自 2026-11-01 ezstar（How to HackStar?）的手工解出与本地复现；flag 字面量已擦除。 -->

# 技术笔记：Web 前端只当传输层 —— 受限 SSRF 打通内网原生服务后的堆利用

## 什么时候用

- 目标是一个 HTTP 前端，唯一像入口的地方是**受限 SSRF**：只允许 `localhost` / `127.x` / `::1`、不跟随重定向，参数名类似 `url=`、`target=`、`fetch=`。
- 内网还有自研服务（自写 HTTP、私有协议、二进制网关），附件或下载包里给了 ELF；`README/runbook` 说明线上就是这份构建。
- 前端把源码或依赖端出来：`express.static(__dirname)` 直接可读 `server.js`、`package.json`、`node_modules/...`。
- 内网服务的回包有固定形态的**窗口输出**（hex dump、调试帧、固定长度回显），可以当泄漏通道。
- 二进制是 C++（`shared_ptr`、虚表、`_Sp_counted_ptr`），缺陷是解码/协议解析把数据写进固定缓冲区。

这类题的顺序是：**先确认代理把什么形状送进内网，再决定逆什么**。反过来先逆二进制、最后才发现 body 送不进去，会白花大量时间。

## 第一步：确认转发语义，别用近似实验下结论

前端常见写法：

```js
req.pipe(request({ url: req.query.url, followRedirect: false })).pipe(res)
```

- 关键在依赖库的 `pipe` 语义。`request@2.88.2` 的 `Request.prototype` 会把源 `IncomingMessage` 的 **method 和 headers 逐条复制**给上游（`self.method = src.method`、`self.headers` 合并），所以客户端的 `POST` + `Content-Length` 会原样到达内网服务。
- **不要**用裸 Node `http.request({method:'GET'})` 的"GET 丢 body"结论代替对实际库的判断——那是另一个实现。
- 验证方法：起一个 dump server 记录原始 head+body，发一个 >1KB 的 body 过去做字节级 `MATCH`；再本地复刻前端（同版本 Node + request）和内网服务，比较直连与经代理两条路径的分配序列/响应是否一致。额外头（`Expect: 100-continue`、`Transfer-Encoding`、`User-Agent`）要单独确认，内网服务若只认 Content-Length，必须确认上游确实拿到了它。

## 第二步：把回包变成泄漏通道

- 固定偏移的 hex dump 往往直接暴露相邻 chunk 的元数据：chunk size、引用计数、对象指针、bin 的 `fd/bk`。先假设"窗口 = 某缓冲区 − 固定前缀"，再用不同请求量验证。
- 本地用分配钩子（LD_PRELOAD 记录 `malloc/free` 大小与地址）把**每请求分配顺序、chunk 邻接关系、free 顺序**摸清；线上第一次请求就核对布局签名（size 字段值、引用计数 `0x100000001` 一类常量、缓冲区 size）。
- 判据必须能失败：连发 3 次，对象指针/bin 指针稳定不变才算模型成立；不稳定就停手，别把服务打崩。
- libc 基址从 bin head 反推：单个 chunk 在 bin 里时 `fd == bk == bin_head = libc + 固定偏移`；不同 bin 的偏移不同，先用页对齐（`base & 0xfff == 0`）筛，再用另一个已知指针交叉验证。

## 第三步：从写到控

- 找写原语：解码/解析循环把数据写进固定缓冲区，规则常是 `decoded[i] → buf[i-shift]`（前几个字节被帧头吃掉）；长度由 body 长度决定，帧头里的 length 不限制输出。
- 相邻对象是 C++ 控制块时：伪造 vtable 指针 + 引用计数，让析构路径的虚调用落到 `setcontext`；ucontext 里按偏移摆好 `rdi/rsi/rdx/rsp/rip/mxcsr/fpstate`，`rip = execve`、`rdi = "/bin/sh"`、`rsi = argv`。
- 偏移按目标 libc 版本核对（`readelf -sW libc.so.6 | grep -E 'setcontext|execve'`），不要背别的发行版的数。
- 回显：accept 模型的服务里子进程继承连接 fd（常见 3=监听、4=连接）。命令前缀 `exec >&4 2>&4;` 把输出写回正在读的连接；不确定就先用 `ls -l /proc/self/fd` 侦察。
- 一次 `execve` 顶掉服务进程：侦察和取 flag 分开打，或写一条足够全面的命令；如果服务被守护自动拉起，每轮重新 leak 地址，不要复用。

## 坑

- **目标二进制的调试环境变量不要开**（如 `SGDEBUG`）：调试打印可能先解引用伪造对象，在 hijack 之前就 SIGSEGV，让人误判"链路不通"。
- 两个控制块可能随 tcache LIFO 交替复用：利用请求前固定 warm-up 次数，并用布局签名确认本次命中的是 B 邻接的那个。
- 容器会过期/重启：URL 502 先确认实例状态；重开后地址全变，必须重新 leak。
- 本地必须跑通同一条链（同版本 Node/request、同 libc）再打远程；远程先 leak 校验再打，失败先分辨是崩在调试路径/错误 cb 还是偏移不对，不要盲目改地址重试。

## 最小验收清单

- [ ] 抓包证明上游收到预期 method + Content-Length + 完整 body（字节级比对）
- [ ] 本地复刻前端+服务，直连/经代理行为一致
- [ ] 布局签名连续 3 次稳定（对象指针与 libc 指针不变）
- [ ] 本地同 libc 上完整链跑通（shell 输出回到原连接）
- [ ] 远程先侦察（`id`、`ls /`、`find / -maxdepth 3 -iname '*flag*'`）再取 flag
