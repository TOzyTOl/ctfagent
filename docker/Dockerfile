# 二进制题的执行环境：gdb + binutils 预装，不再每次 apt-get（今天在容器里临时装
# gdb 时遇到过 502，也白白花掉了最贵的头几分钟）。
#
#   docker build -t ctfagent/kali:latest docker
#   docker run --rm -v <workspace>:/w -w /w ctfagent/kali:latest
#
# 基础镜像用 ubuntu:24.04（glibc 2.39），覆盖本机 WSL 只有 20.04（glibc 2.31）
# 跑不了新编译程序的情况。
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        gdb \
        binutils \
        file \
        xxd \
        strace \
        ltrace \
        patchelf \
        python3 \
        python3-pip \
        git \
        curl \
        unzip \
        p7zip-full \
 && rm -rf /var/lib/apt/lists/*

# pwntools：反汇编/ELF 解析/ROP 都在里面，pwn 方向几乎总要。
# --break-system-packages：Ubuntu 24.04 的 externally-managed 环境限制。
RUN pip3 install --break-system-packages --no-cache-dir pwntools || \
    echo "pwntools 安装失败（网络），gdb/binutils 仍可用"

WORKDIR /work
