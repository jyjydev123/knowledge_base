#!/usr/bin/env bash
# 本地开发入口。
#
#   ./start.sh              只起后端（0.0.0.0:8000 + 热重载）
#   ./start.sh check        启动前体检，不起服务：数据库/embedding/向量车道/记忆车道/Redis
#   ./start.sh api          同默认，只是把意图写明白
#   ./start.sh web          只起前端 Vite dev server（5173，缺 node_modules 会自动 npm install）
#   ./start.sh all          后端另开一个终端窗口，当前终端跑前端（两边日志各看各的）
#   NOTERM=1 ./start.sh all  不开新窗口，两边日志合流到当前终端（[api] / [web] 前缀）
#   TERMINAL=xterm ./start.sh all  指定终端模拟器；默认自己挑（konsole/gnome-terminal/xterm…）
#   NO_RELOAD=1 ./start.sh  关掉热重载（手机联调时更稳）
#   HOST=127.0.0.1 PORT=9000 WEB_PORT=5174 ./start.sh all
#   ./start.sh api --log-level debug    多余参数原样透给 uvicorn（web 模式不透）
#
# 为什么默认 host 是 0.0.0.0 而不是 127.0.0.1：这个项目的终局是装进手机，
# 真机联调（TODO.md §5 M9）必须从另一台设备访问，127.0.0.1 会让你以为"后端起了但手机连不上"。
# 暴露到公网前请先收 HOST 或上反代（TODO.md §6）。
#
# 为什么前端不由后端托管（FastAPI StaticFiles）：开发期要 HMR，而且 vite.config.ts 里的
# /api 代理顺带把 CORS 这一层噪声隔开了。后端 app/ 里没有任何 StaticFiles 挂载，
# 它只会出现在 8000 上的 /docs 和 /api/v1/**，所以以前 ./start.sh 永远起不出页面 —— 不是坏了。
# 想"一条命令一个端口"是部署形态的事（§4 联调稳定后再加 build 产物托管），
# 现在加只会让人分不清自己刚打的是 5173 的代理还是 8000 的直连。
#
# 关于 all 的两个细节，踩过的坑都在这儿：
#   1. 新窗口里的后端**不是本脚本的子进程**，所以 Ctrl+C 当前终端只停前端，
#      要停后端得去那个窗口 Ctrl+C（或直接关窗口）—— 这是故意的，
#      前端崩了不该把带着数据库连接的后端一起带走。
#   2. 起之前先看端口是不是已经被占了：是的话复用现有后端，不会再开一个
#      （第二个必然 Address already in use，弹窗里一行红字闪过去，比这个难查得多）。
set -euo pipefail
cd "$(dirname "$0")"
ROOT_DIR="$(pwd -P)"

VENV_DIR="${VENV_DIR:-.venv}"
PY="$VENV_DIR/bin/python"
UVICORN="$VENV_DIR/bin/uvicorn"
WEB_DIR="${WEB_DIR:-frontend}"
API_HOST="${HOST:-0.0.0.0}"
API_PORT="${PORT:-8000}"
WEB_HOST="${WEB_HOST:-0.0.0.0}"
WEB_PORT="${WEB_PORT:-5173}"
API_LOG="logs/api.log"
TERM_LOG="logs/terminal.log"

# 体检单独走，别混进下面的模式解析
if [[ "${1:-}" == "check" ]]; then
  if [[ ! -x "$PY" ]]; then
    echo "缺少虚拟环境：$PY 不可执行。先用 uv sync 或 python3 -m venv .venv" >&2
    exit 1
  fi
  exec "$PY" scripts/preflight.py
fi

MODE="api"
if [[ $# -gt 0 ]]; then
  case "$1" in
    api | web | all)
      MODE="$1"
      shift
      ;;
    -*) ;; # 第一个就是 uvicorn 参数，交给下面的透传
    *)
      echo "x 未知模式：$1（可用：api / web / all / check；要透传给 uvicorn 请以 - 开头，如 ./start.sh api --log-level debug）" >&2
      exit 2
      ;;
  esac
fi
PASSTHRU=("$@")

require_venv() {
  if [[ ! -x "$UVICORN" ]]; then
    echo "缺少虚拟环境：$UVICORN 不可执行。" >&2
    echo "  推荐：~/.local/bin/uv sync --group dev      （项目根的 .venv）" >&2
    echo "  或者：python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2
    exit 1
  fi
}

warn_no_env() {
  if [[ ! -f .env ]]; then
    echo "! 没有 .env：正在用 app/core/config.py 里的默认值" >&2
    echo "  数据库默认 postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/knowledge_base" >&2
    echo "  LLM/Embedding 的 key 默认是空的 → 向量车道与问答会自动降级（不是坏了，是没配）" >&2
  fi
}

warn_proxy_port() {
  # vite.config.ts 的 proxy target 写死 127.0.0.1:8000；换端口时前端会静默 502，先说清楚
  if [[ "$API_PORT" != "8000" ]]; then
    echo "! 注意：vite 的 /api 代理目标写死在 127.0.0.1:8000（见 frontend/vite.config.ts），" >&2
    echo "  现在后端跑在 $API_PORT → 前端页面会一直报错。要么用默认端口，要么改那个 target。" >&2
  fi
}

uvicorn_args() {
  UVICORN_ARGS=(app.main:app --host "$API_HOST" --port "$API_PORT")
  if [[ "${NO_RELOAD:-0}" != "1" ]]; then
    UVICORN_ARGS+=(--reload)
  fi
  UVICORN_ARGS+=("${PASSTHRU[@]}")
}

port_open() {
  # 探测失败（含 socket 被禁）一律当"没开"，别把 traceback 刷在用户脸上
  "$PY" -c 'import socket, sys; s = socket.socket(); s.settimeout(0.4); sys.exit(s.connect_ex(("127.0.0.1", int(sys.argv[1]))))' "${1:-$API_PORT}" 2>/dev/null
}

wait_for_api() {
  local i
  for ((i = 0; i < 60; i++)); do
    if [[ -n "${API_PID:-}" ]] && ! kill -0 "$API_PID" 2>/dev/null; then
      return 1
    fi
    if port_open; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

cleanup_api() {
  if [[ -n "${TAIL_PID:-}" ]]; then
    kill "$TAIL_PID" 2>/dev/null || true
    wait "$TAIL_PID" 2>/dev/null || true
    TAIL_PID=""
  fi
  [[ -n "${API_PID:-}" ]] || return 0
  kill "$API_PID" 2>/dev/null || true
  wait "$API_PID" 2>/dev/null || true
  API_PID=""
}

detect_terminal() {
  if [[ -n "${TERMINAL:-}" ]]; then
    command -v "$TERMINAL" >/dev/null 2>&1
    return $?
  fi
  local t
  for t in konsole gnome-terminal xfce4-terminal mate-terminal tilix lxterminal qterminal \
    deepin-terminal kitty alacritty wezterm foot urxvt xterm; do
    if command -v "$t" >/dev/null 2>&1; then
      TERMINAL="$t"
      return 0
    fi
  done
  return 1
}

# 新窗口里跑的那条命令：进到项目根、把端口等参数带过去、退出后别自动关窗（不然报错一闪而过）
api_shell_command() {
  local cmd a
  cmd="printf '\\033]0;knowledge_base 后端\\007'; cd $(printf '%q' "$ROOT_DIR") && "
  cmd+="VENV_DIR=$(printf '%q' "$VENV_DIR") HOST=$(printf '%q' "$API_HOST") "
  cmd+="PORT=$(printf '%q' "$API_PORT") NO_RELOAD=$(printf '%q' "${NO_RELOAD:-0}") ./start.sh api"
  for a in "${PASSTHRU[@]}"; do
    cmd+=" $(printf '%q' "$a")"
  done
  cmd+='; ec=$?; printf "\n[api] 后端进程已退出 (exit=%s)，回车关闭本窗口\n" "$ec"; read -r _'
  printf '%s' "$cmd"
}

start_api_in_terminal() {
  [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]] || return 1
  detect_terminal || return 1
  local cmd
  cmd="$(api_shell_command)"
  # 启动器自己的报错（参数不认识 / 连不上显示服务）留在 $TERM_LOG，失败时打给用户看
  case "$TERMINAL" in
    konsole) "$TERMINAL" -e bash -c "$cmd" ;;
    xterm | urxvt) "$TERMINAL" -title kb-api -e bash -c "$cmd" ;;
    gnome-terminal | mate-terminal | xfce4-terminal | lxterminal | qterminal | deepin-terminal | tilix)
      "$TERMINAL" --title kb-api -- bash -c "$cmd"
      ;;
    kitty | foot) "$TERMINAL" --title kb-api bash -c "$cmd" ;;
    alacritty) "$TERMINAL" --title kb-api -- bash -c "$cmd" ;;
    wezterm) "$TERMINAL" start --class kb-api bash -c "$cmd" ;;
    *) return 1 ;;
  esac >"$TERM_LOG" 2>&1 &
  disown 2>/dev/null || true
  return 0
}

start_web() {
  if ! command -v npm >/dev/null 2>&1; then
    echo "x 没找到 npm：前端需要 Node（本机 $(node -v 2>/dev/null || echo 'node 也没有')）。" >&2
    echo "  只有后端的话用 ./start.sh 或 ./start.sh api。" >&2
    exit 1
  fi
  if [[ ! -d "$WEB_DIR" ]]; then
    echo "x 找不到前端目录 $WEB_DIR" >&2
    exit 1
  fi
  if [[ ! -d "$WEB_DIR/node_modules" ]]; then
    echo "-> $WEB_DIR/node_modules 不存在，先装依赖（npm install，可能要几分钟）"
    (cd "$WEB_DIR" && npm install)
  fi
  cd "$WEB_DIR"
}

case "$MODE" in
  api)
    require_venv
    warn_no_env
    uvicorn_args
    echo "=> 后端 http://${API_HOST}:${API_PORT}  （docs: /docs，健康: /health）"
    exec "$UVICORN" "${UVICORN_ARGS[@]}"
    ;;

  web)
    warn_proxy_port
    start_web
    echo "=> 前端 http://${WEB_HOST}:${WEB_PORT}  （/api 已代理到 127.0.0.1:${API_PORT}，所以后端也得起着）"
    exec npm run dev -- --host "$WEB_HOST" --port "$WEB_PORT"
    ;;

  all)
    warn_proxy_port
    require_venv
    warn_no_env
    mkdir -p "$(dirname "$API_LOG")"
    API_IN_WINDOW=0
    API_PID=""
    TAIL_PID=""
    trap 'cleanup_api' EXIT INT TERM

    if port_open; then
      echo "=> $API_PORT 已有后端在跑，复用现有的（不重复起；要换代码请去那个窗口 Ctrl+C 再启动）"
    elif [[ "${NOTERM:-0}" != "1" ]] && start_api_in_terminal; then
      API_IN_WINDOW=1
      echo "=> 后端开在新终端窗口（$TERMINAL）；关掉那个窗口或在里面 Ctrl+C 才停后端"
    else
      uvicorn_args
      "$UVICORN" "${UVICORN_ARGS[@]}" >"$API_LOG" 2>&1 &
      API_PID=$!
      tail -n 0 -F "$API_LOG" 2>/dev/null | sed -u 's/^/[api] /' &
      TAIL_PID=$!
      echo "=> 后端 pid=$API_PID（完整日志 $API_LOG），本窗口同时显示 [api] / [web]"
    fi

    if ! wait_for_api; then
      echo "x 后端没跑起来（${API_PORT} 一直没监听，或进程直接退了），前端就不起了（起了也是整屏报错）。" >&2
      if [[ "$API_IN_WINDOW" == "1" ]]; then
        echo "  去看那个弹出的窗口写了什么。起不来常见两种：没有图形会话 / 那个终端不认识这些参数" >&2
        echo "  —— 先试 NOTERM=1 ./start.sh all（不开窗口，日志直接在本终端）。" >&2
        if [[ -s "$TERM_LOG" ]]; then
          echo "  终端启动器输出：" >&2
          cat "$TERM_LOG" >&2
        fi
      else
        echo "  后端日志最后 20 行（完整在 $API_LOG）：" >&2
        tail -n 20 "$API_LOG" >&2 || true
      fi
      exit 1
    fi

    echo "=> 后端就绪 http://${API_HOST}:${API_PORT}   前端即将打开 http://${WEB_HOST}:${WEB_PORT}"
    start_web
    if [[ "$API_IN_WINDOW" == "1" ]]; then
      npm run dev -- --host "$WEB_HOST" --port "$WEB_PORT"
    else
      npm run dev -- --host "$WEB_HOST" --port "$WEB_PORT" 2>&1 | sed -u 's/^/[web]  /'
    fi
    ;;

  *)
    echo "未知模式：$MODE（可用：api / web / all）" >&2
    exit 2
    ;;
esac
