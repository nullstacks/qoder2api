#!/usr/bin/env bash
# frpc 入口（launchd 监管）：解析 frpc 可执行文件位置后 exec。
#
# 为什么要有这一层，而不是把 frpc 路径直接写进 plist：
#   plist 模板不能写死开发机的绝对路径（仓库要能在任意机器克隆使用），
#   而 frpc 的安装位置随安装方式而变（Homebrew /opt/homebrew/bin、
#   /usr/local/bin，或自行下载解压）。这一层把它收敛成「装在哪都能跑」，
#   plist 因此只需要 __PROJECT_ROOT__ 一个占位符。
#
# 与 scripts/launchd-server.sh 同一取舍：用 exec 替换本进程，确保 launchd
# 拿到的 PID 就是 frpc 本身，kickstart -k 的 SIGTERM 能直达，不留孤儿进程。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/deploy/frp/frpc.toml"

# FRPC_BIN 显式指定优先；否则按 PATH、再按 Homebrew 常见前缀依次找。
FRPC_BIN="${FRPC_BIN:-$(command -v frpc 2>/dev/null || true)}"
if [[ ! -x "$FRPC_BIN" ]]; then
    for candidate in /opt/homebrew/bin/frpc /usr/local/bin/frpc; do
        if [[ -x "$candidate" ]]; then
            FRPC_BIN="$candidate"
            break
        fi
    done
fi

if [[ -z "$FRPC_BIN" || ! -x "$FRPC_BIN" ]]; then
    echo "frpc-run: 找不到 frpc 可执行文件；先 brew install frp，或用 FRPC_BIN 指定路径" >&2
    exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
    echo "frpc-run: 缺少配置文件 $CONFIG" >&2
    exit 1
fi

# 服务器地址由 plist 的 EnvironmentVariables 注入，供 frpc.toml 的
# {{ .Envs.FRP_SERVER_ADDR }} 取用；这里只做存在性提示，不阻断（frpc 会报错）。
echo "frpc-run: 连接 ${FRP_SERVER_ADDR:-<未设置 FRP_SERVER_ADDR>} …"
exec "$FRPC_BIN" -c "$CONFIG"
