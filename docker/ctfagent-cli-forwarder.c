/* ctfagent-cli-forwarder.c — CTFAGENT 沙箱模式的 CLI 转发器。
 *
 * SDK 以 [forwarder.exe, <sdk 参数…>] 方式 spawn 本程序；本程序把同样的参数
 * 原样交给 WSL2 里真正的 claude CLI：
 *
 *   wsl.exe -d <distro> --cd <cwd 的 WSL 路径> --exec <inner cli> <sdk 参数…>
 *
 * 配置经环境变量传入（后端在每次 spawn 前注入，进程内不出机器）：
 *   CTFAGENT_SANDBOX_DISTRO  例如 Ubuntu-20.04
 *   CTFAGENT_SANDBOX_CLI     例如 /usr/local/bin/claude
 *
 * 当前目录由本程序自行换算成 WSL 路径（D:\x\y → /mnt/d/x/y），不依赖环境。
 * stdio 三根句柄原样继承，流的读写行为与直跑 CLI 完全一致。
 *
 * 用 C 而不是脚本：SDK 拒绝以 .bat/.cmd 结尾的 cli_path（命令行重解析漏洞
 * 的防御），wsl.exe 自身又无法在参数前注入 -d/--cd——原生 exe 是唯一能通过
 * SDK 门禁的转发形态。构建：gcc -O2 -municode -o ctfagent-cli-wsl.exe
 * ctfagent-cli-forwarder.c -lshell32
 */

#include <windows.h>
#include <shellapi.h>
#include <stdio.h>
#include <stdlib.h>

static wchar_t *env_w(const wchar_t *name) {
    DWORD n = GetEnvironmentVariableW(name, NULL, 0);
    if (n == 0) return NULL;
    wchar_t *v = malloc((size_t)n * sizeof(wchar_t));
    if (v) GetEnvironmentVariableW(name, v, n);
    return v;
}

/* 当前目录（Windows 形式）→ WSL 形式：D:\a\b → /mnt/d/a/b。调用方释放。 */
static wchar_t *cwd_to_wsl(void) {
    DWORD n = GetCurrentDirectoryW(0, NULL);
    if (n < 2) return NULL;
    wchar_t *dir = malloc((size_t)n * sizeof(wchar_t));
    GetCurrentDirectoryW(n, dir);
    /* 至少是 "X:\"；取盘符字母，其余按 / 拼接 */
    wchar_t drive = (dir[0] >= L'a' && dir[0] <= L'z') ? dir[0] : dir[0] + 32;
    size_t len = n + 16;
    wchar_t *out = malloc(len * sizeof(wchar_t));
    swprintf(out, len, L"/mnt/%lc%ls", drive, dir + 2);  /* 跳过 "X:" */
    for (wchar_t *c = out; *c; c++) if (*c == L'\\') *c = L'/';
    free(dir);
    return out;
}

/* 把一个 argv 元素按 Windows 命令行规则追加到串里（含引号转义）。 */
static void append_arg(wchar_t **buf, size_t *len, const wchar_t *arg) {
    size_t need = wcslen(arg) * 2 + 4;
    wchar_t *q = malloc(need * sizeof(wchar_t));
    wchar_t *w = q;
    *w++ = L'"';
    for (const wchar_t *c = arg; *c; c++) {
        if (*c == L'"') *w++ = L'\\';
        *w++ = *c;
    }
    *w++ = L'"';
    *w = 0;
    size_t curlen = wcslen(*buf);
    wchar_t *nb = realloc(*buf, (curlen + need + 2) * sizeof(wchar_t));
    if (!nb) exit(2);
    *buf = nb;
    swprintf(*buf + curlen, need + 2, L"%ls %ls", (curlen ? L"" : L""), q);
    free(q);
}

int wmain(void) {
    wchar_t *distro = env_w(L"CTFAGENT_SANDBOX_DISTRO");
    wchar_t *cli = env_w(L"CTFAGENT_SANDBOX_CLI");
    if (!distro || !cli) {
        fwprintf(stderr, L"forwarder: CTFAGENT_SANDBOX_DISTRO / _CLI 未设置\n");
        return 87;
    }
    wchar_t *cwd_wsl = cwd_to_wsl();

    int argc = 0;
    LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv || argc < 1) return 2;

    /* 组命令行：wsl.exe -d <distro> --cd <cwd> --exec <cli> <argv[1..]> */
    size_t cap = 64 + wcslen(distro) * 2 + (cwd_wsl ? wcslen(cwd_wsl) * 2 : 0)
               + wcslen(cli) * 2 + 16;
    wchar_t *cmd = malloc(cap * sizeof(wchar_t));
    swprintf(cmd, cap,
             L"\"C:\\Windows\\System32\\wsl.exe\" -d %ls --cd \"%ls\" --exec \"%ls\"",
             distro, cwd_wsl ? cwd_wsl : L"/tmp", cli);
    for (int i = 1; i < argc; i++) append_arg(&cmd, &cap, argv[i]);

    STARTUPINFOW si = { .cb = sizeof(si) };
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(L"C:\\Windows\\System32\\wsl.exe", cmd,
                        NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
        fwprintf(stderr, L"forwarder: wsl 启动失败（%lu）\n", GetLastError());
        return 3;
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    free(distro); free(cli); free(cwd_wsl); free(cmd);
    LocalFree(argv);
    return (int)code;
}
